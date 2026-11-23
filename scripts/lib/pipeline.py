"""Pipeline helpers shared by run_init and run_daily: which lines need work, project status."""

from . import project as proj
from .jsonio import load_json
from .paths import status_file

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
