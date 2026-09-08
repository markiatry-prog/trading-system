#!/usr/bin/env python3
"""Name the mechanism behind every excluded session, and price the rule.

Consumes the anonymised quality_fixture.json produced by
diagnose_quality_gate.py. No prices, no dataset, no network.

TWO SEPARATE QUESTIONS, DELIBERATELY NOT CONFLATED.

  1. DATA QUALITY -- is this session's tape defective? Answered by the
     rule that actually fired in the gate. Every ExclusionRule maps to a
     mechanism here, so "unexplained" can only ever mean a rule this
     script has never heard of, never a rule it simply forgot to
     handle. That distinction is the point: an exclusion nobody can name
     is either a real defect or a gate bug.

  2. CONTRACT CONTINUITY -- may a prior-session level be compared
     against this session? Answered from the symbology labels carried in
     the fixture, by comparing adjacent sessions. This is NOT a quality
     question and never excludes a session; it makes specific
     hypotheses ineligible on specific sessions.

THE EXPIRY CALENDAR APPEARS ONCE, AS A FALSIFIER. The contract labels
come from the vendor's symbology, and the assumption underneath them is
that an instrument id is stable for one contract's life. If that were
false the labels would change constantly and the rule would be resting
on sand. So the transitions the labels imply are compared against the
known quarterly expiries -- to CHECK the provenance, never to supply
it. A disagreement is printed loudly; the calendar never overrides.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.features.roll import expiries_between, is_quarterly_expiry

# Every exclusion rule the gate can fire, and what it means happened.
# Kept exhaustive on purpose: see the module docstring.
RULE_MECHANISM = {
    "no_data": "no tape at all for the session",
    "rth_coverage": "minutes missing from liquid hours",
    "rth_contiguous_gap": "an unbroken run of missing RTH minutes",
    "implausible_print": "a one-minute excursion beyond both neighbours "
                         "that immediately reverted",
    "duplicate_timestamps": "the same minute delivered more than once",
    "out_of_order": "bars not monotonic in time",
    "degraded_source_date": "the vendor flagged this date",
}


def mechanism(day: date, entry: dict) -> tuple:
    """(family, detail) for one excluded session.

    Ordered from the most specific explanation to the least, so a
    truncated expiry session is reported as an expiry rather than
    merely as a coverage shortfall.
    """
    rules = list(entry.get("new_gate_exclusions", []))
    unknown = [r for r in rules if r not in RULE_MECHANISM]
    if unknown:
        return "unexplained", f"gate fired {unknown}, which this script cannot name"

    coverage_like = {"no_data", "rth_coverage", "rth_contiguous_gap"}
    integrity = [r for r in rules if r not in coverage_like]

    # Integrity defects are about the bytes, not the session, so they
    # are named first whatever the calendar says about the day.
    if integrity:
        return ("data_integrity",
                "; ".join(RULE_MECHANISM[r] for r in integrity))

    if is_quarterly_expiry(day):
        return ("quarterly_expiry",
                "the expiring contract settles to a Special Opening "
                "Quotation and stops trading at the cash open, so a "
                "continuous series still pointing at it has almost no "
                "RTH tape")
    if entry.get("expected_rth_minutes", 390) < 390:
        return ("calendar_shortfall",
                f"the session was genuinely short "
                f"({entry['expected_rth_minutes']} expected RTH minutes)")
    if rules:
        return ("feed_gap", "; ".join(RULE_MECHANISM[r] for r in rules))
    return "unexplained", "excluded with no rule recorded"


def contracts_of(entry: dict):
    return tuple(entry.get("contracts") or ())


def continuity(day: date, prior: date, by_day: dict) -> str:
    """The same four-way verdict features.contracts produces, computed
    here from the fixture's labels so the report can be read offline."""
    cur = contracts_of(by_day[day])
    if not cur or "UNKNOWN" in cur:
        return "unknown_provenance"
    if len(cur) > 1:
        return "mixed_session"
    if prior is None:
        return "no_prior_session"
    prv = contracts_of(by_day[prior])
    if not prv or "UNKNOWN" in prv:
        return "unknown_provenance"
    if len(prv) > 1:
        return "mixed_session"
    return "same_contract" if prv[0] == cur[0] else "contract_boundary"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default="quality_fixture.json")
    args = ap.parse_args()

    fixture = json.loads(Path(args.fixture).read_text())
    rows = fixture["sessions"]
    by_day = {date.fromisoformat(r["date"]): r for r in rows}
    days = sorted(by_day)

    excluded = [d for d in days if by_day[d]["new_gate_exclusions"]]
    passed = [d for d in days if not by_day[d]["new_gate_exclusions"]]

    print(f"symbol {fixture['symbol']}   sessions {len(days)}   "
          f"passed {len(passed)}   excluded {len(excluded)}\n")

    # =================================================================
    print("=" * 72)
    print("1. DATA QUALITY -- why each excluded session was excluded")
    print("=" * 72)
    expiries = expiries_between(days[0], days[-1])
    print(f"  quarterly expiries in window: {len(expiries)}")
    missed = [d for d in expiries if d not in set(excluded)]
    if missed:
        print(f"  expiries that PASSED the gate: {[str(d) for d in missed]}")

    print(f"\n  {'date':12} {'wd':4} {'mechanism':20} {'rth':>5} {'cov':>7} "
          f"{'gap':>5}  rules")
    counts = Counter()
    detail = {}
    for d in excluded:
        e = by_day[d]
        fam, why = mechanism(d, e)
        counts[fam] += 1
        detail[d] = (fam, why)
        print(f"  {str(d):12} {d.strftime('%a'):4} {fam:20} "
              f"{e['rth_bars']:>5} {e['rth_coverage']:>7.3f} "
              f"{e['longest_rth_gap']:>5}  {','.join(e['new_gate_exclusions'])}")
    print("\n  " + "  ".join(f"{k}={v}" for k, v in counts.most_common()))

    for d in excluded:
        fam, why = detail[d]
        if fam in ("data_integrity", "feed_gap", "calendar_shortfall",
                   "unexplained"):
            print(f"\n  {d} ({d.strftime('%A')}) -- {fam}")
            print(f"    {why}")
            print(f"    bars={by_day[d]['bars']} rth_bars={by_day[d]['rth_bars']} "
                  f"overnight={by_day[d]['overnight_bars']} "
                  f"zero_volume={by_day[d]['zero_volume_bars']}")
            print(f"    LEGITIMATE EXCLUSION: "
                  f"{'NO -- investigate' if fam == 'unexplained' else 'yes'}")

    unexplained = [d for d in excluded if detail[d][0] == "unexplained"]
    print(f"\n  unexplained after attribution: {len(unexplained)}"
          f"{'  ' + str([str(d) for d in unexplained]) if unexplained else '  (none)'}")

    # =================================================================
    print("\n" + "=" * 72)
    print("2. CONTRACT CONTINUITY -- from symbology, not from the calendar")
    print("=" * 72)
    if not any(contracts_of(by_day[d]) for d in days):
        print("  This fixture predates contract labelling. Re-run")
        print("  diagnose_quality_gate.py to record them, or the")
        print("  contract-boundary rule cannot be audited offline.")
        return 1

    # Prior session = previous session that PASSED, because that is the
    # one whose level the engine actually carries forward.
    verdicts = Counter()
    boundaries = []
    prior = None
    for d in passed:
        v = continuity(d, prior, by_day)
        verdicts[v] += 1
        if v == "contract_boundary":
            boundaries.append((d, prior))
        prior = d

    labels = sorted({c for d in days for c in contracts_of(by_day[d])})
    print(f"  distinct contracts across the window: {len(labels)}")
    for v, n in verdicts.most_common():
        print(f"    {n:>5}  {v}")

    print(f"\n  contract boundaries among passing sessions: {len(boundaries)}")
    for d, prv in boundaries:
        near = min((abs((d - e).days) for e in expiries), default=None)
        gap = f"{near}d from an expiry" if near is not None else "no expiry in window"
        print(f"    {d} ({d.strftime('%a')})  prior session {prv}  "
              f"{contracts_of(by_day[prv])[0]} -> {contracts_of(by_day[d])[0]}"
              f"   [{gap}]")

    # -- the falsifier ------------------------------------------------
    print(f"\n  CROSS-CHECK against the expiry calendar (falsification only):")
    print(f"    expiries in window          {len(expiries)}")
    print(f"    contract transitions seen   {len(boundaries)}")
    if len(boundaries) > len(expiries) * 2:
        print("    MISMATCH: far more transitions than expiries. The vendor's")
        print("    instrument ids are not stable per contract, and the")
        print("    eligibility rule cannot be trusted. STOP and investigate.")
    elif abs(len(boundaries) - len(expiries)) <= 2:
        print("    consistent: transitions track the quarterly cycle, which")
        print("    is what a stable per-contract id should produce.")
    else:
        print("    review: the counts differ by more than rounding at the")
        print("    window edges. Not fatal, but name the difference.")

    # =================================================================
    print("\n" + "=" * 72)
    print("3. WHAT THE RULE COSTS")
    print("=" * 72)
    n = len(days)
    print(f"  sessions excluded by the quality gate      {len(excluded):>5}  "
          f"{len(excluded)/n:6.2%}")
    print(f"  sessions where H6/H7 become ineligible     {len(boundaries):>5}  "
          f"{len(boundaries)/max(1,len(passed)):6.2%} of passing sessions")
    print("  sessions excluded outright by the rule          0   "
          "(it is per-hypothesis)")
    print("\n  Affected: H6, H7 -- both key off prior-session levels.")
    print("  Unaffected: H1-H5, H8-H12 -- every input is intraday, so")
    print("  these sessions stay in their samples in full.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
