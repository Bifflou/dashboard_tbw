"""Daily update of every initialized line, resuming from each line's checkpoint (SPEC §7.2), then dashboards.

Runs after scripts.fetch_fx. Pending lines are left to run_init: only the admin's button starts an initialization.
"""

import sys

from scripts.lib import aggregate, engine, pipeline
from scripts.lib.project import list_project_slugs, load_project, validate_project
from scripts.lib.report import Report


def update_project(slug: str, config: dict, report: Report) -> None:
    pipeline.remove_lines(slug, pipeline.orphan_line_ids(slug, config))
    for token, line in pipeline.initialized_lines(slug, config):
        scope = f"{slug}/{line['id']}"
        try:
            result = engine.process_line(slug, token, line, mode="update")
        except Exception as exc:  # noqa: BLE001 - a failing line never stops the others
            message = f"{type(exc).__name__}: {exc}"
            pipeline.save_line_status(slug, line, error=message)
            report.error(scope, f"update failed, previous data kept: {message}")
            continue
        pipeline.save_line_status(slug, line, result)
        if result.state == "warning":
            report.warning(scope, result.message)
    pending = pipeline.pending_lines(slug, config)
    if pending:
        report.info(slug, f"{len(pending)} line(s) waiting for initialization")
    aggregate.rebuild(slug, config, report)


def main() -> int:
    report = Report()
    slugs = list_project_slugs()
    for slug in slugs:
        config = load_project(slug)
        errors = validate_project(config, expected_slug=slug)
        if errors:
            report.error(slug, "invalid project.json, skipped: " + "; ".join(errors[:5]))
            continue
        update_project(slug, config, report)
    report.info("daily", f"{len(slugs)} project(s) updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
