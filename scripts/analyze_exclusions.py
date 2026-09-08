#!/usr/bin/env python3
"""Classify every excluded session, and quantify what excluding them costs.

Consumes the anonymised quality_fixture.json produced by
diagnose_quality_gate.py. No prices, no dataset, no network.

Each exclusion is assigned to a MECHANISM rather than left as "unusual":

  quarterly_expiry     the third Friday. The expiring contract settles to
                       a Special Opening Quotation and stops trading at
                       the cash open, so a continuous series still
                       pointing at it has almost no RTH tape.
  roll_adjacent        the session after an expiry; its prior-day and
                       overnight levels reach back across a price
                       discontinuity into a different contract.
  calendar_shortfall   a session the calendar thinks is normal but which
                       was genuinely shortened.
  feed_gap             minutes missing from liquid hours, not explained
                       by any of the above.
  unexplained          none of the above. These are the ones that matter
                       most, because an unexplained exclusion is either a
                       real defect or a gate bug, and both need naming.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.features.roll import (
    expiries_between, is_quarterly_expiry, roll_adjacent_sessions)


def classify(day, sessions, entry):
    if is_quarterly_expiry(day):
        return "quarterly_expiry"
    if day in sessions["roll_adjacent"]:
        return "roll_adjacent"
    if entry.get("expected_rth_minutes", 390) < 390:
        return "calendar_shortfall"
    if "rth_contiguous_gap" in entry.get("new_gate_exclusions", []) or \
       "rth_coverage" in entry.get("new_gate_exclusions", []):
        return "feed_gap"
    return "unexplained"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default="quality_fixture.json")
    args = ap.parse_args()

    fixture = json.loads(Path(args.fixture).read_text())
    rows = fixture["sessions"]
    days = [date.fromisoformat(r["date"]) for r in rows]
    by_day = {date.fromisoformat(r["date"]): r for r in rows}
    sessions = {"roll_adjacent": roll_adjacent_sessions(days)}

    excluded = [d for d in days if by_day[d]["new_gate_exclusions"]]
    passed = [d for d in days if not by_day[d]["new_gate_exclusions"]]

    print(f"symbol {fixture['symbol']}   sessions {len(days)}   "
          f"passed {len(passed)}   excluded {len(excluded)}\n")

    expiries = expiries_between(days[0], days[-1])
    print(f"quarterly expiries in window: {len(expiries)}")
    excluded_expiries = [d for d in expiries if d in set(excluded)]
    print(f"  of which excluded: {len(excluded_expiries)}/{len(expiries)}")
    missed = [d for d in expiries if d not in set(excluded)]
    if missed:
        print(f"  expiries that PASSED the gate: {[str(d) for d in missed]}")
        for d in missed:
            e = by_day.get(d)
            if e:
                print(f"    {d}  rth_bars={e['rth_bars']} "
                      f"coverage={e['rth_coverage']:.3f} gap={e['longest_rth_gap']}")

    print("\nMECHANISM FOR EVERY EXCLUDED SESSION")
    print(f"  {'date':12} {'wd':4} {'mechanism':18} {'rth':>5} {'cov':>7} "
          f"{'gap':>5}  rules")
    counts = Counter()
    for d in excluded:
        e = by_day[d]
        mech = classify(d, sessions, e)
        counts[mech] += 1
        print(f"  {str(d):12} {d.strftime('%a'):4} {mech:18} "
              f"{e['rth_bars']:>5} {e['rth_coverage']:>7.3f} "
              f"{e['longest_rth_gap']:>5}  {','.join(e['new_gate_exclusions'])}")

    print("\n  " + "  ".join(f"{k}={v}" for k, v in counts.most_common()))

    unexplained = [d for d in excluded
                   if classify(d, sessions, by_day[d]) == "unexplained"]
    if unexplained:
        print(f"\n  UNEXPLAINED ({len(unexplained)}): {[str(d) for d in unexplained]}")
        print("  These need a named cause before the gate can be signed off.")

    # -- contamination the gate does NOT currently catch ---------------
    print("\n" + "=" * 66)
    print("ROLL-ADJACENT CONTAMINATION (not currently excluded)")
    adjacent = sorted(sessions["roll_adjacent"])
    still_in = [d for d in adjacent if d in set(passed)]
    print(f"  sessions immediately after an expiry: {len(adjacent)}")
    print(f"  of those, currently PASSING the gate: {len(still_in)}")
    print("  On these, prior-day and overnight levels come from the")
    print("  PREVIOUS contract at a different price level. The gap is not")
    print("  a market move, and H6/H7 (prior-day sweeps) key off exactly")
    print("  those levels.")
    if still_in:
        print(f"  first few: {[str(d) for d in still_in[:6]]}")

    # -- materiality ----------------------------------------------------
    print("\n" + "=" * 66)
    print("MATERIALITY")
    n = len(days)
    print(f"  excluded now                     {len(excluded):>5}  "
          f"{len(excluded)/n:6.2%} of sessions")
    print(f"  roll-adjacent, if also excluded  {len(still_in):>5}  "
          f"{len(still_in)/n:6.2%}")
    combined = len(set(excluded) | set(still_in))
    print(f"  combined                         {combined:>5}  {combined/n:6.2%}")
    print("\n  Effect on the preregistered set, by hypothesis family:")
    print("    H1-H3  ORB            intraday only  -> unaffected by roll levels")
    print("    H4-H5  displacement   intraday only  -> unaffected")
    print("    H8-H9  structure      intraday only  -> unaffected")
    print("    H10-H12 FVG           intraday only  -> unaffected")
    print("    H6-H7  PD sweeps      REACH ACROSS THE ROLL -> affected")
    print(f"\n  A sweep hypothesis draws on ~1 event per session at most, so")
    print(f"  {len(still_in)} contaminated sessions is ~{len(still_in)/max(1,len(passed)):.1%} of the")
    print("  sweep sample. Small, but the contamination is not random: a")
    print("  cross-contract gap manufactures apparent sweeps of levels that")
    print("  were never traded, which biases H6/H7 in one direction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
