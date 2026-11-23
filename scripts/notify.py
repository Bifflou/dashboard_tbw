"""Send the end-of-run Discord message (SPEC §7.4).

- init  : always (success or failure)
- daily : only when the run failed or reported an error / warning

Never fails the workflow: a missing DISCORD_WEBHOOK_URL or a Discord error only prints a line.

Usage: python -m scripts.notify --job-status success|failure|cancelled [--run-url URL]
"""

import argparse
import os
import sys

import requests

from scripts.lib.jsonio import load_json
from scripts.lib.paths import REPORT_FILE

COLORS = {"ok": 0x2EA043, "warning": 0xD29922, "error": 0xDA3633}
MAX_LINES = 15


def site_url() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def build_message(report: dict, job_status: str, run_url: str | None) -> dict | None:
    workflow = report.get("workflow", "local")
    entries = report.get("entries", [])
    errors = [e for e in entries if e["level"] == "error"]
    warnings = [e for e in entries if e["level"] == "warning"]
    failed = job_status not in ("success", "")

    if workflow == "daily" and not (failed or errors or warnings):
        return None

    level = "error" if failed or errors else "warning" if warnings else "ok"
    hint = report.get("project_hint")
    if workflow == "init":
        title = f"Initialization {'failed' if level == 'error' else 'finished'}" + (f" — {hint}" if hint else "")
    else:
        title = "Daily update: " + ("failed" if failed else "issues found")

    lines: list[str] = []
    initialized = report.get("initialized", [])
    if initialized:
        lines.append(f"**{len(initialized)} line(s) initialized**")
        for item in initialized[:MAX_LINES]:
            lines.append(f"• `{item['project']}/{item['line_id']}` {item['first_date']} → {item['last_date']}")
    elif workflow == "init":
        lines.append("No line was initialized.")
    for label, group in (("Errors", errors), ("Warnings", warnings)):
        if group:
            lines.append(f"**{label} ({len(group)})**")
            lines.extend(f"• `{e['scope']}` {e['message']}" for e in group[:MAX_LINES])
            if len(group) > MAX_LINES:
                lines.append(f"… and {len(group) - MAX_LINES} more")
    if failed:
        lines.append(f"Workflow status: **{job_status}**")

    links = []
    base = site_url()
    if base and hint and workflow == "init":
        links.append(f"[Open dashboard]({base}dashboard.html?p={hint})")
    if run_url:
        links.append(f"[Run logs]({run_url})")
    if links:
        lines.append(" · ".join(links))

    description = "\n".join(lines)
    if len(description) > 4000:
        description = description[:3990] + "\n…"
    return {
        "username": "Dashboard builder",
        "embeds": [{"title": title, "description": description, "color": COLORS[level]}],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-status", default="success")
    parser.add_argument("--run-url", default=None)
    args = parser.parse_args()

    report = load_json(REPORT_FILE) or {
        "workflow": os.environ.get("PIPELINE_WORKFLOW", "local"),
        "project_hint": os.environ.get("PIPELINE_PROJECT_HINT") or None,
        "entries": [{"level": "error", "scope": "workflow", "message": "the run stopped before writing its report"}],
    }
    message = build_message(report, args.job_status, args.run_url)
    if message is None:
        print("Nothing to notify.")
        return 0

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("DISCORD_WEBHOOK_URL not set: notification skipped.")
        return 0
    try:
        resp = requests.post(webhook, json=message, timeout=15)
        if resp.status_code >= 400:
            print(f"Discord answered HTTP {resp.status_code}: notification not delivered.")
        else:
            print("Discord notification sent.")
    except requests.RequestException as exc:
        print(f"Discord unreachable ({type(exc).__name__}): notification not delivered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
