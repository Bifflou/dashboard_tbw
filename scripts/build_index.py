"""Build data/projects.json, the index read by the public home page."""

import sys

from scripts.lib import pipeline
from scripts.lib.dates import utc_now_iso
from scripts.lib.jsonio import load_json, save_json_if_changed
from scripts.lib.paths import PROJECTS_INDEX_FILE, dashboard_file
from scripts.lib.project import list_project_slugs, load_project, validate_project
from scripts.lib.report import Report


def project_entry(slug: str, config: dict) -> dict:
    dashboard = load_json(dashboard_file(slug)) or {}
    image = config.get("image")
    entry = {
        "slug": slug,
        "name": config["name"],
        "image": f"projects/{slug}/{image}" if image else None,
        "status": pipeline.project_state(slug, config),
        "last_date": dashboard.get("last_date"),
        "default_currency": dashboard.get("default_currency"),
        "mcap": None,
        "holders": None,
        "chains": sorted({line["chain"] for token in config["tokens"] for line in token["lines"]}),
        "tokens": [token["symbol"] for token in config["tokens"]],
    }
    if dashboard.get("dates"):
        entry["mcap"] = {ccy: _last(series["total"]) for ccy, series in dashboard.get("mcap", {}).items()}
        entry["holders"] = _last(dashboard.get("holders", {}).get("total", []))
    return entry


def _last(values: list):
    return next((v for v in reversed(values) if v is not None), None)


def main() -> int:
    report = Report()
    projects = []
    for slug in list_project_slugs():
        config = load_project(slug)
        if validate_project(config, expected_slug=slug):
            continue  # already reported by run_init / run_daily
        projects.append(project_entry(slug, config))
    projects.sort(key=lambda p: p["name"].lower())
    written = save_json_if_changed(
        PROJECTS_INDEX_FILE,
        {"schema_version": 1, "generated_at": utc_now_iso(), "projects": projects},
    )
    report.info("index", f"{len(projects)} project(s) listed ({'updated' if written else 'unchanged'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
