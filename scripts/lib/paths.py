"""Repository paths. Every script resolves files from here, never from the current directory.

The engine check (scripts/check_engine.py) redirects projects and data to tests/output/ with the
DASHBOARD_PROJECTS_DIR and DASHBOARD_DATA_DIR environment variables, so fixtures never touch real data.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config"
CHAINS_FILE = CONFIG_DIR / "chains.json"

PROJECTS_DIR = Path(os.environ.get("DASHBOARD_PROJECTS_DIR") or ROOT / "projects")  # written by the admin only
DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR") or ROOT / "data")
FX_FILE = DATA_DIR / "fx" / "rates.json"
PROJECTS_INDEX_FILE = DATA_DIR / "projects.json"
PROJECTS_DATA_DIR = DATA_DIR / "projects"

RUN_DIR = ROOT / ".run"                   # not committed
REPORT_FILE = RUN_DIR / "report.json"


def project_config_file(slug: str) -> Path:
    return PROJECTS_DIR / slug / "project.json"


def project_data_dir(slug: str) -> Path:
    return PROJECTS_DATA_DIR / slug


def line_series_file(slug: str, line_id: str) -> Path:
    return project_data_dir(slug) / "lines" / f"{line_id}.json"


def line_state_file(slug: str, line_id: str) -> Path:
    return project_data_dir(slug) / "lines" / f"{line_id}.state.json"


def nav_file(slug: str, token_id: str) -> Path:
    return project_data_dir(slug) / "nav" / f"{token_id}.json"


def status_file(slug: str) -> Path:
    return project_data_dir(slug) / "status.json"


def dashboard_file(slug: str) -> Path:
    return project_data_dir(slug) / "dashboard.json"
