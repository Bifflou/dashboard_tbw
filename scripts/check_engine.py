"""Engine check on real data, run by .github/workflows/engine-check.yml with the API keys (SPEC §11).

For each fixture tests/fixtures/*.json (only those whose chains have an adapter):
  1. full initialization of every line up to D-1, then aggregation (dashboard.json);
  2. comparison with the reference values of the old dashboards (soge / spiko);
  3. resume check: init up to D-12, update to D-6 (twice), update to D-1 -> must equal the full init.

Everything is written under tests/output/ (ignored by Git), never in data/. Results are printed as GitHub
annotations, which can be read through the public API, plus a plain log. Exit code 1 on any error.

Usage: python -m scripts.check_engine [fixture-name ...]
"""

import shutil
import sys
import time
import traceback
from datetime import timedelta

from scripts.adapters import get_adapter
from scripts.lib import aggregate, engine, fx, paths, pipeline
from scripts.lib.dates import last_complete_day
from scripts.lib.jsonio import load_json
from scripts.lib.project import iter_lines, validate_project

FIXTURES_DIR = paths.ROOT / "tests" / "fixtures"
OUTPUT_DIR = paths.ROOT / "tests" / "output"
SUPPLY_TOLERANCE_PCT = 0.01
RESUME_OFFSETS = (12, 6, 6, 0)   # init at D-12, update at D-6 twice (idempotence), update at D-1

errors = 0


def annotate(level: str, title: str, message: str) -> None:
    global errors
    if level == "error":
        errors += 1
    print(f"[{level.upper()}] {title}: {message}", flush=True)
    esc = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    esc_title = title.replace("%", "%25").replace(":", "%3A").replace(",", "%2C")
    print(f"::{level} title={esc_title}::{esc}", flush=True)


def use_data_dir(name: str) -> None:
    """Point the engine at tests/output/<name>/ with a copy of the real FX file."""
    root = OUTPUT_DIR / name
    if root.exists():
        shutil.rmtree(root)
    (root / "fx").mkdir(parents=True)
    real_fx = paths.ROOT / "data" / "fx" / "rates.json"
    if real_fx.exists():
        shutil.copy(real_fx, root / "fx" / "rates.json")
    paths.PROJECTS_DATA_DIR = root / "projects"
    fx.FX_FILE = root / "fx" / "rates.json"


def compare_reference(series: dict, reference: list[dict]) -> tuple[str, str]:
    by_day = {d["date"]: d for d in series["days"]}
    worst_supply, holder_diffs, missing, worst_example = 0.0, 0, 0, ""
    for ref in reference:
        ours = by_day.get(ref["date"])
        if ours is None:
            missing += 1
            continue
        diff = abs(ours["supply"] - ref["supply"]) / ref["supply"] * 100 if ref["supply"] else abs(ours["supply"])
        if diff > worst_supply:
            worst_supply = diff
            worst_example = f"{ref['date']}: {ours['supply']:,.2f} vs {ref['supply']:,.2f}"
        if ours["holders"] != ref["holders"]:
            holder_diffs += 1
            worst_example += f" | holders {ref['date']}: {ours['holders']} vs {ref['holders']}"
    level = "notice" if worst_supply <= SUPPLY_TOLERANCE_PCT and holder_diffs == 0 and missing == 0 else "warning"
    message = (f"{len(reference)} reference days: max supply diff {worst_supply:.4f}%, "
               f"{holder_diffs} holder mismatch(es), {missing} missing day(s)")
    if worst_example:
        message += f". Worst: {worst_example[:300]}"
    return level, message


def _sources(adapter) -> str:
    stats = getattr(adapter, "stats", None) or {}
    text = ", ".join(f"{k}={v}" for k, v in stats.items() if k != "node_errors")
    errors = stats.get("node_errors") or []
    return text + (f"; last node error: {errors[-1]}" if errors else "")


def check_fixture(path) -> None:
    fixture = load_json(path)
    project = fixture["project"]
    slug = project["slug"]
    name = path.stem
    problems = validate_project(project)
    if problems:
        annotate("error", f"{name} config", "; ".join(problems[:5]))
        return

    last_day = last_complete_day()
    lines = list(iter_lines(project))

    # 1-2. Full initialization and reference comparison.
    use_data_dir(f"{name}/full")
    full = {}
    for token, line in lines:
        label = f"{name} {token['symbol']} {line['chain']}"
        started = time.monotonic()
        adapter = get_adapter(line)
        try:
            result = engine.process_line(slug, token, line, mode="init", last_day=last_day, adapter=adapter)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            annotate("error", f"{label} init", f"{type(exc).__name__}: {exc}")
            continue
        pipeline.save_line_status(slug, line, result)
        series = load_json(paths.line_series_file(slug, line["id"]))
        state = load_json(paths.line_state_file(slug, line["id"]))
        full[line["id"]] = (series["days"], state["holder_periods"])
        last = series["days"][-1]
        rec = result.reconciliation
        rec_text = f"on-chain {rec['onchain']:,.2f}, diff {rec['diff_pct']:.4f}%" if rec else "no on-chain value"
        annotate("warning" if result.state != "ok" else "notice", f"{label} init",
                 f"{len(series['days'])} days {series['first_date']} -> {series['last_date']}; "
                 f"last supply {last['supply']:,.2f}, holders {last['holders']}; {rec_text}; "
                 f"{time.monotonic() - started:.0f}s; sources {_sources(adapter)}")
        reference = fixture.get("reference", {}).get("lines", {}).get(line["id"])
        if reference:
            level, message = compare_reference(series, reference)
            annotate(level, f"{label} vs reference", message)

    if len(full) == len(lines):
        try:
            dash = aggregate.build_dashboard(slug, project)
            totals = ", ".join(f"{c} {dash['mcap'][c]['total'][-1]:,.0f}" for c in dash["currencies"])
            annotate("notice", f"{name} dashboard",
                     f"{len(dash['dates'])} days; market cap {totals}; holders {dash['holders']['total'][-1]} "
                     f"(by token {sum(v[-1] or 0 for v in dash['holders']['by_token'].values())}); "
                     f"default currency {dash['default_currency']}")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            annotate("error", f"{name} dashboard", f"{type(exc).__name__}: {exc}")

    # 3. Resume: init in the past, then daily updates, must equal the full init.
    use_data_dir(f"{name}/resume")
    for token, line in lines:
        if line["id"] not in full:
            continue
        label = f"{name} {token['symbol']} {line['chain']} resume"
        try:
            for step, offset in enumerate(RESUME_OFFSETS):
                engine.process_line(slug, token, line, mode="init" if step == 0 else "update",
                                    last_day=last_day - timedelta(days=offset), reconcile=False)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            annotate("error", label, f"{type(exc).__name__}: {exc}")
            continue
        days = load_json(paths.line_series_file(slug, line["id"]))["days"]
        periods = load_json(paths.line_state_file(slug, line["id"]))["holder_periods"]
        full_days, full_periods = full[line["id"]]
        if days == full_days and periods == full_periods:
            annotate("notice", label, "init D-12 + updates D-6, D-6, D-1 == full init (series and holder periods)")
        else:
            first_diff = next((a["date"] for a, b in zip(days, full_days) if a != b), "length")
            annotate("error", label, f"resumed data differs from the full init (first difference: {first_diff})")


def main() -> int:
    wanted = set(sys.argv[1:])
    fixtures = sorted(p for p in FIXTURES_DIR.glob("*.json") if not wanted or p.stem in wanted)
    if not fixtures:
        annotate("error", "engine check", "no fixture found")
    for path in fixtures:
        check_fixture(path)
    print(f"Done: {errors} error(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
