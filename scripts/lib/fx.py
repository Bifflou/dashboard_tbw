"""Global FX rates shared by every project (data/fx/rates.json), from Frankfurter (ECB reference rates).

Rates are stored with EUR as base, for every calendar day. Days without an ECB publication (weekends,
TARGET holidays) carry the last published rate forward.
"""

from datetime import date

from . import http
from .dates import add_days, day_range, fmt_day, last_complete_day, parse_day, utc_now_iso
from .jsonio import load_json, save_json_if_changed
from .paths import FX_FILE

SCHEMA_VERSION = 1
API_URL = "https://api.frankfurter.dev/v1"
BASE = "EUR"
QUOTES = ("CHF", "GBP", "USD")          # EUR is the base (rate 1)
SUPPORTED = (BASE, *QUOTES)
DEFAULT_START = date(2019, 1, 1)
REFRESH_DAYS = 3                        # always re-download the last N published days
SEED_DAYS = 10                          # look back this far to find a rate to carry into the first day


class FxError(RuntimeError):
    pass


# ── Reading ──────────────────────────────────────────────────────────────────────

def load_rates() -> dict:
    data = load_json(FX_FILE)
    if not data:
        raise FxError("data/fx/rates.json is missing: run `python -m scripts.fetch_fx` first")
    return data


def rate(rates: dict, day: str, from_ccy: str, to_ccy: str) -> float:
    """Multiplier converting an amount in from_ccy into to_ccy on that day."""
    if from_ccy == to_ccy:
        return 1.0
    try:
        row = rates["rates"][day]
    except KeyError as exc:
        raise FxError(f"No FX rate for {day}") from exc
    return _unit(row, to_ccy) / _unit(row, from_ccy)


def convert(rates: dict, day: str, amount: float, from_ccy: str, to_ccy: str) -> float:
    return amount * rate(rates, day, from_ccy, to_ccy)


def _unit(row: dict, ccy: str) -> float:
    if ccy == BASE:
        return 1.0
    if ccy not in row:
        raise FxError(f"Unsupported currency {ccy}")
    return row[ccy]


# ── Updating ─────────────────────────────────────────────────────────────────────

def fill_calendar(published: dict[str, dict], start: date, end: date, seed: dict | None = None) -> dict[str, dict]:
    """Return one row per calendar day from start to end, carrying the last known rate forward.

    `seed` is the row to carry into the first days if no publication precedes them in `published`.
    """
    calendar: dict[str, dict] = {}
    last = seed
    for day in sorted(d for d in published if d < fmt_day(start)):
        last = published[day]
    for day in day_range(start, end):
        key = fmt_day(day)
        if key in published:
            last = published[key]
        if last is None:
            raise FxError(f"No published rate on or before {key}")
        calendar[key] = {q: last[q] for q in QUOTES}
    return calendar


def fetch_published(start: date, end: date) -> dict[str, dict]:
    """ECB publications between start and end (business days only), fetched one year at a time."""
    published: dict[str, dict] = {}
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(date(chunk_start.year, 12, 31), end)
        payload = http.get_json(
            f"{API_URL}/{fmt_day(chunk_start)}..{fmt_day(chunk_end)}",
            params={"base": BASE, "symbols": ",".join(QUOTES)},
            allow_404=True,  # a chunk with no business day at all (e.g. 1 January alone)
        ) or {}
        for day, row in (payload.get("rates") or {}).items():
            if all(isinstance(row.get(q), (int, float)) for q in QUOTES):
                published[day] = {q: float(row[q]) for q in QUOTES}
        chunk_start = add_days(chunk_end, 1)
    return published


def update_rates(start: date | None = None, end: date | None = None) -> tuple[dict, bool]:
    """Bring data/fx/rates.json up to `end` (default D-1) and back to `start` if needed.

    Returns (rates, written). Only the missing range plus the last REFRESH_DAYS published days are downloaded.
    """
    end = end or last_complete_day()
    existing = load_json(FX_FILE)

    if existing:
        old_start = parse_day(existing["start"])
        wanted_start = min(start, old_start) if start else old_start
        calendar = dict(existing["rates"])
        last_published = parse_day(existing["last_published"])
        # Backward extension (a token older than the current coverage).
        if wanted_start < old_start:
            back = fetch_published(add_days(wanted_start, -SEED_DAYS), add_days(old_start, -1))
            calendar.update(fill_calendar(back, wanted_start, add_days(old_start, -1)))
        # Forward refresh: from the last publication minus a safety margin.
        refresh_from = max(add_days(min(last_published, end), -REFRESH_DAYS), wanted_start)
        seed = calendar.get(fmt_day(add_days(refresh_from, -1)))
        fresh = fetch_published(refresh_from if seed else add_days(refresh_from, -SEED_DAYS), end)
        calendar = {d: r for d, r in calendar.items() if d < fmt_day(refresh_from)}
        calendar.update(fill_calendar(fresh, refresh_from, end, seed=seed))
        published_days = [d for d in fresh] or [existing["last_published"]]
        last_published_str = max(max(published_days), existing["last_published"])
    else:
        wanted_start = start or DEFAULT_START
        fresh = fetch_published(add_days(wanted_start, -SEED_DAYS), end)
        if not fresh:
            raise FxError("Frankfurter returned no rates")
        calendar = fill_calendar(fresh, wanted_start, end)
        last_published_str = max(fresh)

    calendar = {d: calendar[d] for d in sorted(calendar) if fmt_day(wanted_start) <= d <= fmt_day(end)}
    _check_complete(calendar, wanted_start, end)

    data = {
        "schema_version": SCHEMA_VERSION,
        "source": f"{API_URL} (ECB reference rates)",
        "base": BASE,
        "currencies": list(QUOTES),
        "start": fmt_day(wanted_start),
        "end": fmt_day(end),
        "last_published": min(last_published_str, fmt_day(end)),
        "updated_at": utc_now_iso(),
        "rates": calendar,
    }
    written = save_json_if_changed(FX_FILE, data)
    return data, written


def ensure_coverage(start: date, end: date) -> dict:
    """Return the rates, downloading only if [start, end] is not covered yet."""
    data = load_json(FX_FILE)
    if data and data["start"] <= fmt_day(start) and data["end"] >= fmt_day(end):
        return data
    data, _ = update_rates(start=start, end=max(end, last_complete_day()))
    if data["start"] > fmt_day(start) or data["end"] < fmt_day(end):
        raise FxError(f"FX rates do not cover {fmt_day(start)} -> {fmt_day(end)}")
    return data


def _check_complete(calendar: dict, start: date, end: date) -> None:
    missing = [fmt_day(d) for d in day_range(start, end) if fmt_day(d) not in calendar]
    if missing:
        raise FxError(f"FX calendar has {len(missing)} missing days (first: {missing[0]})")
