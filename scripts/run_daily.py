"""Daily update of every initialized line, resuming from each line's checkpoint (SPEC §7.2).

Runs after scripts.fetch_fx. Pending lines are left to run_init. Chain adapters, NAV and aggregation arrive in
steps 2-4 of SPEC §11; until then this script only validates the project configs.
"""

import sys

from scripts.lib import pipeline
from scripts.lib.project import list_project_slugs, load_project, validate_project
from scripts.lib.report import Report


def main() -> int:
    report = Report()
    slugs = list_project_slugs()
    for slug in slugs:
        config = load_project(slug)
        errors = validate_project(config, expected_slug=slug)
        if errors:
            report.error(slug, "invalid project.json, skipped: " + "; ".join(errors[:5]))
            continue
        for line_id in pipeline.orphan_line_ids(slug, config):
            report.info(f"{slug}/{line_id}", "line removed from config, its data will be deleted (step 2)")
    report.info("daily", f"{len(slugs)} project(s) checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
