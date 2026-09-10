#!/usr/bin/env python3
"""Price a Databento historical pull. DOWNLOADS NOTHING.

Covers two questions now:

  T-004 (settled)  the one-minute pull that was bought for $19.25.
  T-006 (open)     the minimum HIGH-RESOLUTION pull needed to research
                   the scalp brackets one-minute bars cannot adjudicate.

WHY A HIGH-RESOLUTION PULL IS EVEN BEING PRICED. The resolution
diagnostic found that in the 09:30-10:50 ET window a 5-point symmetric
bracket has both sides touched inside ONE minute in about 26% of
resolved cases, and 7.5 points in about 11%. Ordering is unknowable
there, so the lower half of the operator's stated range cannot be
researched on one-minute data at all. 10 points is marginal, 15 and 20
are fine.

WHY THE WINDOWS END AT THE DISCOVERY BOUNDARY. The sealed holdout runs
to the end of the acquired period. Buying high-resolution data over
those dates would put a finer view of the final sample on disk, which
is most of the way to having looked at it. Every candidate below
therefore ends on 2024-03-01, inside discovery.

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
SPENT = 19.25                      # the T-004 pull, already charged

# T-004: settled, priced here only so the remaining credit is visible.
CANDIDATES = [
    ("5-year (bought)", "2021-09-01", "2026-09-01"),
    ("3-year (fallback)", "2023-09-01", "2026-09-01"),
    ("1-year NQ only (floor)", "2025-09-01", "2026-09-01"),
]

# T-006: NQ only, and every window ends inside discovery so the sealed
# holdout stays unbought as well as unread.
HIGH_RESOLUTION = [
    ("trades, 3 months", "trades", "2023-12-01", "2024-03-01"),
    ("trades, 6 months", "trades", "2023-09-01", "2024-03-01"),
    ("ohlcv-1s, 3 months", "ohlcv-1s", "2023-12-01", "2024-03-01"),
    ("ohlcv-1s, 6 months", "ohlcv-1s", "2023-09-01", "2024-03-01"),
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

    # -- T-006: the high-resolution comparison -----------------------
    print("\n" + "=" * 74)
    print("T-006 CANDIDATES -- minimum high-resolution NQ pull")
    print("=" * 74)
    print("NQ.c.0 only. Every window ends 2024-03-01, inside discovery, so")
    print("the sealed holdout is not bought either.\n")
    print(f"{'candidate':22} {'schema':10} {'records':>14} {'cost USD':>10} "
          f"{'% credit left':>14}")
    print("-" * 74)
    remaining = CREDIT - SPENT
    high = []
    for label, schema, start, end in HIGH_RESOLUTION:
        row = {"label": label, "schema": schema, "start": start, "end": end}
        try:
            cost = float(client.metadata.get_cost(
                dataset=DATASET, schema=schema, symbols=["NQ.c.0"],
                stype_in=STYPE_IN, start=start, end=end))
            row["cost"] = cost
        except Exception as exc:                  # noqa: BLE001
            row["cost"] = None
            row["cost_error"] = f"{type(exc).__name__}: {exc}"
        try:
            count = int(client.metadata.get_record_count(
                dataset=DATASET, schema=schema, symbols=["NQ.c.0"],
                stype_in=STYPE_IN, start=start, end=end))
            row["records"] = count
        except Exception as exc:                  # noqa: BLE001
            row["records"] = None
            row["count_error"] = f"{type(exc).__name__}: {exc}"
        high.append(row)
        cost_s = "  ERROR" if row["cost"] is None else f"{row['cost']:>10.2f}"
        rec_s = "ERROR" if row["records"] is None else f"{row['records']:>14,}"
        pct_s = ("" if row["cost"] is None
                 else f"{100.0 * row['cost'] / remaining:>13.1f}%")
        print(f"{label:22} {schema:10} {rec_s} {cost_s} {pct_s}")
        for key in ("cost_error", "count_error"):
            if key in row:
                print(f"    {key}: {row[key]}")

    print(f"\ncredit: ${CREDIT:.2f} granted, ${SPENT:.2f} spent on T-004, "
          f"${remaining:.2f} remaining.")
    print("\nSCHEMA NOTE, which the prices alone do not tell you:")
    print("  trades    every execution, nanosecond stamped. First touch is")
    print("            EXACT at any bracket. Also lets any bar interval be")
    print("            rebuilt later, so it never needs buying twice.")
    print("  ohlcv-1s  one-second OHLC. Ambiguity survives only when both")
    print("            sides are touched inside the SAME SECOND, which is")
    print("            far rarer than inside the same minute -- but it is")
    print("            not zero, and it cannot be reconstructed away.")

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
    print("get_cost and get_record_count are metadata calls; neither moves")
    print("market data. Send these figures back for explicit approval before")
    print("any pull.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
