"""Initialize every pending line of every project (full history rebuild), then rebuild their dashboards.

All pending lines are processed, not only those of the project that triggered the run: GitHub keeps at most one
queued run per concurrency group, so a queued init can be replaced by a later one. Processing everything pending
makes that harmless (SPEC §7.1).
"""

import sys

from scripts.lib import aggregate, engine, pipeline
from scripts.lib.project import list_project_slugs, load_project, validate_project
from scripts.lib.report import Report


def init_project(slug: str, config: dict, report: Report) -> int:
    """Initialize the pending lines of one project. Returns the number of lines attempted."""
    orphans = pipeline.orphan_line_ids(slug, config)
    pipeline.remove_lines(slug, orphans)
    pending = pipeline.pending_lines(slug, config)
    for token, line in pending:
        scope = f"{slug}/{line['id']}"
        try:
            result = engine.process_line(slug, token, line, mode="init")
        except Exception as exc:  # noqa: BLE001 - a failing line never stops the others
            message = f"{type(exc).__name__}: {exc}"
            pipeline.save_line_status(slug, line, error=message)
            report.error(scope, f"initialization failed: {message}")
            continue
        pipeline.save_line_status(slug, line, result)
        report.initialized(slug, line["id"], result.first_date, result.last_date)
        if result.state == "warning":
            report.warning(scope, result.message)
    if pending or orphans:
        aggregate.rebuild(slug, config, report)
    return len(pending)


def main() -> int:
    report = Report()
    slugs = list_project_slugs()
    attempted = 0
    for slug in slugs:
        config = load_project(slug)
        errors = validate_project(config, expected_slug=slug)
        if errors:
            report.error(slug, "invalid project.json, skipped: " + "; ".join(errors[:5]))
            continue
        attempted += init_project(slug, config, report)
    if attempted == 0:
        report.info("init", f"nothing to initialize ({len(slugs)} project(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
