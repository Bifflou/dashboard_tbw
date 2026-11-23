"""JSON read/write helpers: atomic writes, stable formatting, and no-op writes when only timestamps changed."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

# Keys that change on every run without the content really changing.
VOLATILE_KEYS = ("generated_at", "updated_at")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def save_json(path: Path, data: Any) -> None:
    """Write atomically (temp file + rename) so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(dumps(data))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _strip_volatile(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: _strip_volatile(v) for k, v in data.items() if k not in VOLATILE_KEYS}
    if isinstance(data, list):
        return [_strip_volatile(v) for v in data]
    return data


def save_json_if_changed(path: Path, data: Any) -> bool:
    """Write only if the content differs once timestamps are ignored. Returns True if written."""
    existing = load_json(path)
    if existing is not None and _strip_volatile(existing) == _strip_volatile(data):
        return False
    save_json(path, data)
    return True
