"""Update the global FX file data/fx/rates.json (shared by every project).

Usage:
    python -m scripts.fetch_fx                    # up to D-1, incremental
    python -m scripts.fetch_fx --start 2018-06-01 # also extend the coverage backwards
"""

import argparse
import sys

from scripts.lib.dates import parse_day
from scripts.lib.fx import FxError, update_rates
from scripts.lib.http import HttpError
from scripts.lib.report import Report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="first day to cover (YYYY-MM-DD)")
    args = parser.parse_args()

    report = Report()
    try:
        data, written = update_rates(start=parse_day(args.start) if args.start else None)
    except (FxError, HttpError) as exc:
        report.error("fx", f"FX update failed, previous rates kept: {exc}")
        return 1
    state = "updated" if written else "unchanged"
    report.info("fx", f"rates {state}: {data['start']} -> {data['end']} (last ECB publication {data['last_published']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
