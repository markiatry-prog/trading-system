#!/usr/bin/env python3
"""Price the T-004 Phase B historical pull. DOWNLOADS NOTHING.

WHY THIS IS A SCRIPT AND NOT A PASTED COMMAND

It is version-controlled, reviewable, and guarded by a test asserting it
contains no data-transfer call. A pasted one-liner has none of those
properties, and the one thing that must not happen here is an
accidental spend.

WHAT IT CALLS
  metadata.list_unit_prices()   free
  metadata.get_cost()           free
Both are metadata operations. Neither transfers market data.

WHAT IT DOES NOT CALL
  timeseries.get_range()        THIS is what costs money.
  batch.submit_job()
Absent by construction, and `test_cost_script_cannot_download` fails the
build if either ever appears.

USAGE
  pip install databento
  export TRADING_DATABENTO_API_KEY=...      # PowerShell: $env:TRADING_...
  python3 scripts/databento_cost_estimate.py
"""
from __future__ import annotations

import os
import sys

DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"
SYMBOLS = ["NQ.c.0", "MNQ.c.0", "ES.c.0"]
STYPE_IN = "continuous"
CREDIT = 125.00

# The two candidates from the Phase B request. Five years is preferred on
# power grounds; three is the fallback if the price is material.
CANDIDATES = [
    ("5-year (recommended)", "2021-09-01", "2026-09-01"),
    ("3-year (fallback)", "2023-09-01", "2026-09-01"),
    ("1-year NQ only (floor)", "2025-09-01", "2026-09-01"),
]


def main() -> int:
    key = os.environ.get("TRADING_DATABENTO_API_KEY")
    if not key:
        print("TRADING_DATABENTO_API_KEY is not set in this environment.",
              file=sys.stderr)
        return 2
    try:
        import databento as db
    except ImportError:
        print("databento is not installed. Run: pip install databento",
              file=sys.stderr)
        return 2

    client = db.Historical(key)

    print(f"dataset {DATASET}   schema {SCHEMA}   stype_in {STYPE_IN}")
    print(f"symbols {', '.join(SYMBOLS)}\n")

    try:
        prices = client.metadata.list_unit_prices(dataset=DATASET)
        print("unit prices (free metadata call):")
        print(f"  {prices}\n")
    except Exception as exc:                      # noqa: BLE001
        print(f"  could not list unit prices: {type(exc).__name__}: {exc}\n")

    print(f"{'candidate':26} {'symbols':>8} {'cost USD':>10} {'% of $125':>10}")
    print("-" * 58)
    results = []
    for label, start, end in CANDIDATES:
        syms = ["NQ.c.0"] if "NQ only" in label else SYMBOLS
        try:
            cost = client.metadata.get_cost(
                dataset=DATASET, schema=SCHEMA, symbols=syms,
                stype_in=STYPE_IN, start=start, end=end,
            )
            pct = 100.0 * float(cost) / CREDIT
            print(f"{label:26} {len(syms):>8} {float(cost):>10.2f} {pct:>9.1f}%")
            results.append((label, float(cost), pct))
        except Exception as exc:                  # noqa: BLE001
            print(f"{label:26} {len(syms):>8}   ERROR {type(exc).__name__}: {exc}")

    if results:
        print("\nrecommendation:")
        five = next((r for r in results if r[0].startswith("5-year")), None)
        if five and five[2] <= 33.0:
            print(f"  proceed with the 5-year pull: ${five[1]:.2f} is "
                  f"{five[2]:.1f}% of the credit, within the one-third limit.")
        elif five:
            three = next((r for r in results if r[0].startswith("3-year")), None)
            print(f"  5-year is ${five[1]:.2f} ({five[2]:.1f}%), ABOVE the "
                  f"one-third limit I committed to.")
            if three:
                print(f"  fall back to 3-year: ${three[1]:.2f} ({three[2]:.1f}%).")

    print("\nNothing was downloaded. No charge has been incurred.")
    print("Send these figures back for explicit approval before any pull.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
