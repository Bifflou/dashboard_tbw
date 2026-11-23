"""Run report (.run/report.json, not committed): collected by pipeline scripts, read by scripts/notify.py."""

import os

from .dates import utc_now_iso
from .jsonio import load_json, save_json
from .paths import REPORT_FILE


class Report:
    """Append-only report shared by every script of one workflow run."""

    def __init__(self) -> None:
        self.data = load_json(REPORT_FILE) or {
            "workflow": os.environ.get("PIPELINE_WORKFLOW", "local"),
            "request_id": os.environ.get("PIPELINE_REQUEST_ID") or None,
            "project_hint": os.environ.get("PIPELINE_PROJECT_HINT") or None,
            "started_at": utc_now_iso(),
            "entries": [],
            "initialized": [],
        }

    def _add(self, level: str, scope: str, message: str) -> None:
        self.data["entries"].append({"level": level, "scope": scope, "message": message})
        line = f"[{level.upper()}] {scope}: {message}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # Windows consoles without UTF-8
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        self.save()

    def info(self, scope: str, message: str) -> None:
        self._add("info", scope, message)

    def warning(self, scope: str, message: str) -> None:
        self._add("warning", scope, message)

    def error(self, scope: str, message: str) -> None:
        self._add("error", scope, message)

    def initialized(self, project: str, line_id: str, first_date: str | None, last_date: str | None) -> None:
        self.data["initialized"].append(
            {"project": project, "line_id": line_id, "first_date": first_date, "last_date": last_date}
        )
        self.save()

    def has(self, level: str) -> bool:
        return any(e["level"] == level for e in self.data["entries"])

    def save(self) -> None:
        save_json(REPORT_FILE, self.data)
