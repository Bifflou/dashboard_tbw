"""Project aggregation: build data/projects/<slug>/dashboard.json (docs/FORMATS.md §8, SPEC §4.3-4.4).

Only initialized lines are included (pending lines, whose config changed since their data was built, are left out).
A line whose last update failed keeps its last known values carried forward, so totals do not collapse.
"""

from . import chains, fx, pipeline
from .dates import day_range, fmt_day, parse_day, utc_now_iso
from .jsonio import load_json, save_json_if_changed
from .paths import dashboard_file, line_series_file, line_state_file, nav_file
from .project import CURRENCIES, pricing_mode

SCHEMA_VERSION = 1


class AggregateError(RuntimeError):
    pass


def rebuild(slug: str, config: dict, report) -> dict | None:
    """Rebuild dashboard.json, reporting (not raising) any failure."""
    try:
        return build_dashboard(slug, config)
    except Exception as exc:  # noqa: BLE001 - one project must never stop the others
        report.error(slug, f"dashboard build failed: {type(exc).__name__}: {exc}")
        return None


def build_dashboard(slug: str, config: dict) -> dict | None:
    included = pipeline.initialized_lines(slug, config)
    path = dashboard_file(slug)
    if not included:
        if path.exists():
            path.unlink()
        return None

    series = {line["id"]: load_json(line_series_file(slug, line["id"])) for _, line in included}
    missing = [line_id for line_id, s in series.items() if not s or not s.get("days")]
    if missing:
        raise AggregateError(f"missing series for {', '.join(missing)}")

    first = min(s["first_date"] for s in series.values())
    last = max(s["last_date"] for s in series.values())
    dates = [fmt_day(d) for d in day_range(parse_day(first), parse_day(last))]
    position = {d: i for i, d in enumerate(dates)}
    rates = fx.ensure_coverage(parse_day(first), parse_day(last))

    tokens = [t for t in config["tokens"] if any(l["id"] in series for l in t["lines"])]
    currencies = [c for c in CURRENCIES if any(t["currency"] == c for t in tokens)]

    supply_by_line, holders_by_line = {}, {}
    for _, line in included:
        days = series[line["id"]]["days"]
        supply_by_line[line["id"]] = _align(days, "supply", position, len(dates))
        holders_by_line[line["id"]] = _align(days, "holders", position, len(dates))

    prices = {t["id"]: _prices(slug, t, dates) for t in tokens}

    mcap = {}
    for ccy in currencies:
        by_line = {}
        for token in tokens:
            factors = [fx.rate(rates, d, token["currency"], ccy) for d in dates]
            for line in token["lines"]:
                if line["id"] not in supply_by_line:
                    continue
                by_line[line["id"]] = [
                    round(s * p * f, 2) if s is not None and p is not None else None
                    for s, p, f in zip(supply_by_line[line["id"]], prices[token["id"]], factors)
                ]
        mcap[ccy] = _group(by_line, tokens, len(dates))

    holders = _group(holders_by_line, tokens, len(dates))
    holders["by_chain"] = {
        chain_id: _unique_holders(slug, [l["id"] for _, l in included if l["chain"] == chain_id], position, dates)
        for chain_id in holders["by_chain"]
    }
    holders["total"] = _sum_lists(list(holders["by_chain"].values()), len(dates))

    dashboard = {
        "schema_version": SCHEMA_VERSION,
        "slug": slug,
        "name": config["name"],
        "image": _site_path(slug, config.get("image")),
        "generated_at": utc_now_iso(),
        "last_date": last,
        "currencies": currencies,
        "default_currency": _default_currency(mcap, tokens, currencies, rates, last),
        "chains": [
            {"id": c, "name": chains.get_chain(c)["name"], "logo": chains.get_chain(c)["logo"]}
            for c in holders["by_chain"]
        ],
        "tokens": [
            {
                "id": t["id"], "name": t["name"], "symbol": t["symbol"], "logo": _site_path(slug, t.get("logo")),
                "currency": t["currency"], "category": t["category"], "nav_type": t.get("nav_type"),
                "lines": [
                    {"id": l["id"], "chain": l["chain"], "explorer_url": _explorer_url(l)}
                    for l in t["lines"] if l["id"] in series
                ],
            }
            for t in tokens
        ],
        "dates": dates,
        "price": {t: [round(p, 8) if p is not None else None for p in values] for t, values in prices.items()},
        "supply": {"by_line": supply_by_line},
        "mcap": mcap,
        "holders": holders,
    }
    save_json_if_changed(path, dashboard, compact=True)
    return dashboard


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _align(days: list[dict], key: str, position: dict, size: int) -> list:
    """Values on the project calendar: None before the line starts, last value carried after it stops."""
    values = [None] * size
    last_index, last_value = -1, None
    for day in days:
        index = position.get(day["date"])
        if index is not None:
            values[index] = day[key]
            last_index, last_value = index, day[key]
    for index in range(last_index + 1, size):
        values[index] = last_value
    return values


def _prices(slug: str, token: dict, dates: list[str]) -> list:
    if pricing_mode(token) == "peg":
        return [1.0] * len(dates)
    nav = load_json(nav_file(slug, token["id"])) or {}
    by_day = {d["date"]: d["nav"] for d in nav.get("days", [])}
    values, last = [], None
    for day in dates:
        last = by_day.get(day, last)
        values.append(last)
    return values


def _sum_lists(lists: list[list], size: int) -> list:
    totals = []
    for index in range(size):
        present = [values[index] for values in lists if values[index] is not None]
        totals.append(round(sum(present), 2) if present else None)
    return totals


def _group(by_line: dict, tokens: list[dict], size: int) -> dict:
    by_token, by_chain_lines = {}, {}
    for token in tokens:
        lines = [l for l in token["lines"] if l["id"] in by_line]
        by_token[token["id"]] = _sum_lists([by_line[l["id"]] for l in lines], size)
        for line in lines:
            by_chain_lines.setdefault(line["chain"], []).append(by_line[line["id"]])
    by_chain = {chain: _sum_lists(lists, size) for chain, lists in by_chain_lines.items()}
    return {
        "total": _sum_lists(list(by_chain.values()), size),
        "by_token": by_token,
        "by_chain": by_chain,
        "by_line": by_line,
    }


def _unique_holders(slug: str, line_ids: list[str], position: dict, dates: list[str]) -> list:
    """Addresses holding at least one of the lines' tokens, counted once per day (SPEC §4.3)."""
    intervals: dict[str, list[tuple[int, int]]] = {}
    last_index = len(dates) - 1
    for line_id in line_ids:
        state = load_json(line_state_file(slug, line_id)) or {}
        for address, periods in state.get("holder_periods", {}).items():
            for start, end in periods:
                s = position.get(start, 0 if start < dates[0] else None)
                e = last_index if end is None else position.get(end, last_index if end > dates[-1] else None)
                if s is None or e is None or e < s:
                    continue
                intervals.setdefault(address, []).append((s, e))

    delta = [0] * (len(dates) + 1)
    for spans in intervals.values():
        spans.sort()
        merged_start, merged_end = spans[0]
        for s, e in spans[1:]:
            if s <= merged_end + 1:
                merged_end = max(merged_end, e)
            else:
                delta[merged_start] += 1
                delta[merged_end + 1] -= 1
                merged_start, merged_end = s, e
        delta[merged_start] += 1
        delta[merged_end + 1] -= 1

    counts, running = [], 0
    first_index = min((s for spans in intervals.values() for s, _ in spans), default=0)
    for index in range(len(dates)):
        running += delta[index]
        counts.append(running if index >= first_index else None)
    return counts


def _default_currency(mcap: dict, tokens: list[dict], currencies: list[str], rates: dict, last: str) -> str:
    """The token currency carrying the largest market cap on the last day."""
    if len(currencies) == 1:
        return currencies[0]
    reference = currencies[0]
    weights = {c: 0.0 for c in currencies}
    for token in tokens:
        value = mcap[reference]["by_token"][token["id"]][-1]
        if value is not None:
            weights[token["currency"]] += value
    return max(currencies, key=lambda c: weights[c])


def _explorer_url(line: dict) -> str:
    template = chains.get_chain(line["chain"])["explorer_token_url"]
    return template.format(**line["identifier"])


def _site_path(slug: str, path: str | None) -> str | None:
    return f"projects/{slug}/{path}" if path else None
