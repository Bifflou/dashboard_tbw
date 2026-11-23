"""XRPL adapter for issued currencies (IOU trust lines). MPT tokens are out of the MVP (SPEC §12).

History: `account_tx` on the issuer, oldest first. Every IOU balance change of the currency goes through a trust
line with the issuer (RippleState), so every one of them is in the issuer's transactions, including transfers
between holders, DEX and AMM trades. Balance changes are read from the transaction **metadata** (previous and
final RippleState balances), which also covers partial payments, transfer fees and clawbacks.

Each holder balance change becomes a Transfer: an increase is a mint to the holder, a decrease a burn from it. The
reconstructed supply is then the sum of holder balances, which is what the issuer owes (gateway_balances
"obligations").

Only full-history nodes are used (node_urls in config/chains.json): a partial-history node would silently answer
with only part of the history.
"""

from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from scripts.lib import chains, http

from .base import Adapter, Transfer

RIPPLE_EPOCH = 946_684_800
SCALE_DECIMALS = 15        # IOU amounts have 15 significant digits; stored as integers of 10^-15
PAGE_LIMIT = 400


class XrplError(RuntimeError):
    pass


def to_raw(value: str) -> int:
    return int(Decimal(value).scaleb(SCALE_DECIMALS).to_integral_value(rounding=ROUND_HALF_EVEN))


class XrplAdapter(Adapter):
    family = "xrpl"

    def __init__(self, line: dict) -> None:
        super().__init__(line)
        self.issuer = line["identifier"]["issuer"]
        self.currency = line["identifier"]["currency"]
        self.node_urls = chains.get_chain(self.chain_id)["xrpl"]["node_urls"]
        self._head: int | None = None
        self.stats = {"pages": 0, "transactions": 0, "balance_changes": 0}

    # ── Adapter interface ────────────────────────────────────────────────────

    def decimals(self) -> int:
        return SCALE_DECIMALS

    def start_position(self) -> dict:
        return {"ledger": 0}

    def non_holder_addresses(self) -> set[str]:
        return {self.issuer}

    def fetch_transfers(self, position: dict) -> list[Transfer]:
        self._head = int(self._call("ledger", {"ledger_index": "validated"})["ledger_index"])
        start = int(position.get("ledger", 0)) or -1
        transfers: list[Transfer] = []
        marker = None
        while True:
            params = {"account": self.issuer, "ledger_index_min": start, "ledger_index_max": self._head,
                      "forward": True, "limit": PAGE_LIMIT}
            if marker:
                params["marker"] = marker
            result = self._call("account_tx", params)
            self.stats["pages"] += 1
            for entry in result.get("transactions", []):
                transfers.extend(self._transfers_of(entry))
            marker = result.get("marker")
            if not marker:
                return transfers

    def onchain_supply_raw(self) -> int | None:
        result = self._call("gateway_balances", {"account": self.issuer, "ledger_index": self._head or "validated"})
        return to_raw((result.get("obligations") or {}).get(self.currency, "0"))

    def onchain_balances_raw(self) -> dict[str, int] | None:
        balances: dict[str, int] = {}
        marker = None
        while True:
            params = {"account": self.issuer, "ledger_index": self._head or "validated", "limit": PAGE_LIMIT}
            if marker:
                params["marker"] = marker
            result = self._call("account_lines", params)
            for trust_line in result.get("lines", []):
                if trust_line.get("currency") == self.currency:
                    holder_balance = -to_raw(trust_line["balance"])  # the issuer's side is negative
                    if holder_balance:
                        balances[trust_line["account"]] = holder_balance
            marker = result.get("marker")
            if not marker:
                return balances

    # ── Parsing ──────────────────────────────────────────────────────────────

    def _transfers_of(self, entry: dict) -> list[Transfer]:
        tx = entry.get("tx") or {}
        meta = entry.get("meta") or {}
        if not isinstance(meta, dict) or meta.get("TransactionResult") != "tesSUCCESS":
            return []
        self.stats["transactions"] += 1
        ledger = int(tx.get("ledger_index") or entry.get("ledger_index"))
        day = datetime.fromtimestamp(int(tx["date"]) + RIPPLE_EPOCH, tz=timezone.utc).strftime("%Y-%m-%d")
        cursor = {"ledger": ledger + 1}
        transfers = []
        for holder, delta in self._balance_changes(meta).items():
            if delta > 0:
                transfers.append(Transfer(day, None, holder, delta, cursor))
            elif delta < 0:
                transfers.append(Transfer(day, holder, None, -delta, cursor))
        self.stats["balance_changes"] += len(transfers)
        return transfers

    def _balance_changes(self, meta: dict) -> dict[str, int]:
        """Holder balance change of this currency in one transaction, from the RippleState nodes."""
        changes: dict[str, int] = {}
        for node in meta.get("AffectedNodes", []):
            kind, body = next(iter(node.items()))
            if body.get("LedgerEntryType") != "RippleState":
                continue
            final = body.get("FinalFields") or body.get("NewFields") or {}
            previous = body.get("PreviousFields") or {}
            balance = final.get("Balance") or previous.get("Balance")
            if not isinstance(balance, dict) or balance.get("currency") != self.currency:
                continue
            low = (final.get("LowLimit") or {}).get("issuer")
            high = (final.get("HighLimit") or {}).get("issuer")
            if self.issuer not in (low, high):
                continue
            holder, sign = (low, 1) if high == self.issuer else (high, -1)  # Balance is the low side's view
            after = to_raw(final["Balance"]["value"]) if "Balance" in final else 0
            if kind == "CreatedNode":
                before = 0
            elif "Balance" in previous:
                before = to_raw(previous["Balance"]["value"])
            else:
                before = after
            delta = sign * (after - before)
            if delta:
                changes[holder] = changes.get(holder, 0) + delta
        return changes

    # ── Node ─────────────────────────────────────────────────────────────────

    def _call(self, method: str, params: dict) -> dict:
        payload = {"method": method, "params": [{**params, "api_version": 1}]}
        errors = []
        for url in self.node_urls:
            try:
                answer = http.post_json(url, payload, timeout=60, max_attempts=3)
            except http.HttpError as exc:
                errors.append(str(exc))
                continue
            result = (answer or {}).get("result", {})
            if result.get("status") == "success":
                return result
            errors.append(f"{url}: {result.get('error_message') or result.get('error')}")
        raise XrplError(f"{method} failed on every node: {'; '.join(errors)[:300]}")
