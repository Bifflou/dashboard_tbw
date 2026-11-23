"""Line engine: rebuild (init) or resume (update) the daily series of one line from its transfers.

SPEC §4.1-4.3 (days, supply, holders) and §7.2 (checkpoint 3 days behind the last day).
Format of the files written here: docs/FORMATS.md §4 and §5.
"""

from dataclasses import dataclass
from datetime import date

from scripts.adapters import Adapter, Transfer, get_adapter

from . import pipeline
from .dates import add_days, day_range, fmt_day, last_complete_day, parse_day, utc_now_iso
from .jsonio import load_json, save_json
from .paths import line_series_file, line_state_file

SCHEMA_VERSION = 1
CHECKPOINT_LAG_DAYS = 3
RECONCILIATION_TOLERANCE_PCT = 0.01


class LineEngineError(RuntimeError):
    pass


@dataclass
class LineResult:
    line_id: str
    state: str                    # "ok" or "warning"
    first_date: str | None
    last_date: str | None
    days_written: int
    reconciliation: dict | None
    message: str | None = None


def process_line(
    slug: str,
    token: dict,
    line: dict,
    *,
    mode: str,
    last_day: date | None = None,
    adapter: Adapter | None = None,
    reconcile: bool = True,
) -> LineResult:
    """mode="init": full rebuild from the first transfer. mode="update": resume from the saved checkpoint."""
    if mode not in ("init", "update"):
        raise ValueError(mode)
    last_day = last_day or last_complete_day()
    adapter = adapter or get_adapter(line)
    fingerprint = pipeline.line_fingerprint(line)
    series_path, state_path = line_series_file(slug, line["id"]), line_state_file(slug, line["id"])

    state = None
    if mode == "update":
        state = load_json(state_path)
        if not state or state.get("fingerprint") != fingerprint:
            raise LineEngineError("no saved state matching the current config: the line must be initialized")

    if state and state["checkpoint"]["date"]:
        checkpoint = state["checkpoint"]
        decimals = state["decimals"]
        cp_date: str | None = checkpoint["date"]
        position = checkpoint["position"]
        balances = {a: int(v) for a, v in checkpoint["balances"].items()}
        series = load_json(series_path) or {"days": []}
        kept_days = [d for d in series["days"] if d["date"] <= cp_date]
        periods = truncate_periods(state["holder_periods"], cp_date)
    else:
        decimals = state["decimals"] if state else adapter.decimals()
        cp_date = None
        position = adapter.start_position()
        checkpoint = {"date": None, "position": position, "supply_raw": "0", "balances": {}}
        balances, kept_days, periods = {}, [], {}

    transfers = adapter.fetch_transfers(position)
    if cp_date:
        transfers = [t for t in transfers if t.day > cp_date]

    excluded = set(line.get("excluded_addresses", []))
    non_holders = adapter.non_holder_addresses() | excluded
    replay = _Replay(balances, periods, excluded, non_holders, decimals)

    last_key = fmt_day(last_day)
    if cp_date:
        start_day = add_days(parse_day(cp_date), 1)
    elif transfers and transfers[0].day <= last_key:
        start_day = parse_day(transfers[0].day)
    else:
        raise LineEngineError(
            f"no transfer found up to {last_key}: check the chain and the contract / issuer of this line"
        )

    checkpoint_key = fmt_day(add_days(last_day, -CHECKPOINT_LAG_DAYS))
    cursor = position
    new_days: list[dict] = []
    index = 0
    for day in day_range(start_day, last_day):
        key = fmt_day(day)
        touched: set[str] = set()
        while index < len(transfers) and transfers[index].day <= key:
            transfer = transfers[index]
            if transfer.day < key:
                raise LineEngineError(f"transfers out of order around {transfer.day}")
            replay.apply(transfer, touched)
            cursor = transfer.cursor
            index += 1
        replay.close_day(key, touched)
        new_days.append({"date": key, "supply": replay.circulating(), "holders": len(replay.holding)})
        if key == checkpoint_key:
            checkpoint = replay.snapshot(key, cursor)

    reconciliation = None
    if reconcile:
        reconciliation = _reconcile(adapter, replay.total_raw, transfers[index:], decimals)

    days = kept_days + new_days
    now = utc_now_iso()
    save_json(series_path, {
        "schema_version": SCHEMA_VERSION,
        "line_id": line["id"],
        "token_id": token["id"],
        "chain": line["chain"],
        "fingerprint": fingerprint,
        "decimals": decimals,
        "first_date": days[0]["date"] if days else None,
        "last_date": days[-1]["date"] if days else None,
        "updated_at": now,
        "days": days,
    }, compact=True)
    save_json(state_path, {
        "schema_version": SCHEMA_VERSION,
        "line_id": line["id"],
        "fingerprint": fingerprint,
        "decimals": decimals,
        "checkpoint": checkpoint,
        "holder_periods": replay.periods,
    }, compact=True)

    state_label, message = "ok", None
    if reconciliation and reconciliation["diff_pct"] > RECONCILIATION_TOLERANCE_PCT:
        state_label = "warning"
        message = (
            f"reconstructed supply {reconciliation['reconstructed']:,.2f} differs from on-chain "
            f"{reconciliation['onchain']:,.2f} ({reconciliation['diff_pct']:.4f}%)"
        )
    return LineResult(
        line_id=line["id"],
        state=state_label,
        first_date=days[0]["date"] if days else None,
        last_date=days[-1]["date"] if days else None,
        days_written=len(new_days),
        reconciliation=reconciliation,
        message=message,
    )


class _Replay:
    """Balances and holder periods, advanced transfer by transfer and closed day by day."""

    def __init__(self, balances: dict, periods: dict, excluded: set, non_holders: set, decimals: int) -> None:
        self.balances = balances
        self.periods = periods
        self.excluded = excluded
        self.non_holders = non_holders
        self.scale = 10 ** decimals
        self.total_raw = sum(balances.values())
        self.excluded_raw = sum(balances.get(a, 0) for a in excluded)
        self.holding = {a for a, b in balances.items() if b > 0 and a not in non_holders}
        open_in_periods = {a for a, p in periods.items() if p and p[-1][1] is None}
        if open_in_periods != self.holding:
            raise LineEngineError("saved state is inconsistent (holders vs periods): re-initialize the line")

    def apply(self, t: Transfer, touched: set) -> None:
        if t.amount < 0:
            raise LineEngineError("negative transfer amount")
        if t.sender is None:
            self.total_raw += t.amount
        else:
            self._move(t.sender, -t.amount, t.day)
            touched.add(t.sender)
        if t.receiver is None:
            self.total_raw -= t.amount
        else:
            self._move(t.receiver, t.amount, t.day)
            touched.add(t.receiver)

    def _move(self, address: str, delta: int, day: str) -> None:
        balance = self.balances.get(address, 0) + delta
        if balance < 0:
            raise LineEngineError(f"negative balance for {address} on {day}: transfers are missing")
        if balance:
            self.balances[address] = balance
        else:
            self.balances.pop(address, None)
        if address in self.excluded:
            self.excluded_raw += delta

    def close_day(self, key: str, touched: set) -> None:
        previous = None
        for address in touched:
            if address in self.non_holders:
                continue
            holds = self.balances.get(address, 0) > 0
            if holds and address not in self.holding:
                self.holding.add(address)
                self.periods.setdefault(address, []).append([key, None])
            elif not holds and address in self.holding:
                self.holding.discard(address)
                previous = previous or fmt_day(add_days(parse_day(key), -1))
                self.periods[address][-1][1] = previous

    def circulating(self) -> float:
        return round((self.total_raw - self.excluded_raw) / self.scale, 6)

    def snapshot(self, key: str, cursor: dict) -> dict:
        return {
            "date": key,
            "position": cursor,
            "supply_raw": str(self.total_raw),
            "balances": {a: str(b) for a, b in self.balances.items()},
        }


def truncate_periods(periods: dict, cp_date: str) -> dict:
    """Holder periods as they stood at the end of cp_date (a period still running then is reopened)."""
    truncated: dict[str, list] = {}
    for address, address_periods in periods.items():
        kept = []
        for start, end in address_periods:
            if start > cp_date:
                continue
            kept.append([start, None if end is None or end >= cp_date else end])
        if kept:
            truncated[address] = kept
    return truncated


def _reconcile(adapter: Adapter, total_raw: int, later: list[Transfer], decimals: int) -> dict | None:
    """Compare the reconstructed supply *now* (last day + today's mints/burns) with the on-chain supply."""
    onchain = adapter.onchain_supply_raw()
    if onchain is None:
        return None
    reconstructed = total_raw
    for t in later:
        if t.sender is None:
            reconstructed += t.amount
        if t.receiver is None:
            reconstructed -= t.amount
    scale = 10 ** decimals
    diff_pct = abs(onchain - reconstructed) / onchain * 100 if onchain else (0.0 if reconstructed == 0 else 100.0)
    return {
        "onchain": round(onchain / scale, 6),
        "reconstructed": round(reconstructed / scale, 6),
        "diff_pct": round(diff_pct, 6),
    }
