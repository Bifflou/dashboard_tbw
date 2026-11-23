"""Pipeline helpers shared by run_init and run_daily: which lines need work, line status, cleanup."""

from . import project as proj
from .dates import utc_now_iso
from .jsonio import load_json, save_json
from .paths import line_series_file, line_state_file, status_file

STATE_RANK = {"pending": 0, "ok": 1, "warning": 2, "error": 3}


def load_status(slug: str) -> dict:
    return load_json(status_file(slug)) or {"schema_version": 1, "project": slug, "lines": {}}


def line_fingerprint(line: dict) -> str:
    return proj.fingerprint(line["chain"], line["identifier"], line.get("excluded_addresses", []))


def pending_lines(slug: str, config: dict) -> list[tuple[dict, dict]]:
    """(token, line) pairs that must be (re)initialized: never initialized, or config changed since.

    A line whose last run failed is not pending: the daily run retries it from its checkpoint.
    If it never produced data (failed at init), it has no fingerprint in status.json and stays pending.
    """
    status_lines = load_status(slug).get("lines", {})
    pending = []
    for token, line in proj.iter_lines(config):
        entry = status_lines.get(line["id"])
        if not entry or not entry.get("last_date") or entry.get("fingerprint") != line_fingerprint(line):
            pending.append((token, line))
    return pending


def initialized_lines(slug: str, config: dict) -> list[tuple[dict, dict]]:
    pending_ids = {line["id"] for _, line in pending_lines(slug, config)}
    return [(t, l) for t, l in proj.iter_lines(config) if l["id"] not in pending_ids]


def orphan_line_ids(slug: str, config: dict) -> list[str]:
    """Lines that have data but are no longer in the config (deleted or identifier changed)."""
    configured = {line["id"] for _, line in proj.iter_lines(config)}
    return sorted(set(load_status(slug).get("lines", {})) - configured)


def save_line_status(slug: str, line: dict, result=None, *, error: str | None = None) -> None:
    """Record the outcome of a line run in status.json.

    On success the entry takes the current fingerprint. On error the previous fingerprint and last_date are kept,
    so a failing update keeps its data (and is retried) while a failing init stays pending.
    """
    status = load_status(slug)
    previous = status["lines"].get(line["id"], {})
    now = utc_now_iso()
    if error is None:
        entry = {
            "fingerprint": line_fingerprint(line),
            "state": result.state,
            "last_run": now,
            "last_date": result.last_date,
            "message": result.message,
            "reconciliation": result.reconciliation,
        }
    else:
        entry = {
            "fingerprint": previous.get("fingerprint"),
            "state": "error",
            "last_run": now,
            "last_date": previous.get("last_date"),
            "message": error[:500],
            "reconciliation": previous.get("reconciliation"),
        }
    status["lines"][line["id"]] = entry
    status["updated_at"] = now
    save_json(status_file(slug), status)


def remove_lines(slug: str, line_ids: list[str]) -> None:
    """Delete the data of lines that left the config (deleted, or identifier changed)."""
    if not line_ids:
        return
    for line_id in line_ids:
        for path in (line_series_file(slug, line_id), line_state_file(slug, line_id)):
            if path.exists():
                path.unlink()
    status = load_status(slug)
    for line_id in line_ids:
        status["lines"].pop(line_id, None)
    status["updated_at"] = utc_now_iso()
    save_json(status_file(slug), status)


def project_state(slug: str, config: dict) -> str:
    """Worst state across the project's lines; 'pending' if nothing is initialized yet."""
    status_lines = load_status(slug).get("lines", {})
    states = []
    for _, line in proj.iter_lines(config):
        entry = status_lines.get(line["id"])
        states.append(entry.get("state", "pending") if entry and entry.get("last_date") else "pending")
    initialized = [s for s in states if s != "pending"]
    if not initialized:
        return "pending"
    return max(initialized, key=lambda s: STATE_RANK.get(s, 0))
