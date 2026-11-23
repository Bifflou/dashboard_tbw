"""Initialize every pending line of every project (full history rebuild).

All pending lines are processed, not only those of the project that triggered the run: GitHub keeps at most one
queued run per concurrency group, so a queued init can be replaced by a later one. Processing everything pending
makes that harmless.

The chain adapters arrive in steps 2-3 of SPEC §11. Until then pending lines are only listed.
"""

import sys

from scripts.lib import pipeline
from scripts.lib.project import list_project_slugs, load_project, validate_project
from scripts.lib.report import Report


def main() -> int:
    report = Report()
    slugs = list_project_slugs()
    total_pending = 0
    for slug in slugs:
        config = load_project(slug)
        errors = validate_project(config, expected_slug=slug)
        if errors:
            report.error(slug, "invalid project.json, skipped: " + "; ".join(errors[:5]))
            continue
        pending = pipeline.pending_lines(slug, config)
        total_pending += len(pending)
        for token, line in pending:
            report.warning(f"{slug}/{line['id']}", f"{line['chain']} adapter not implemented yet (SPEC §11 step 2-3)")
    if total_pending == 0:
        report.info("init", f"nothing to initialize ({len(slugs)} project(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
