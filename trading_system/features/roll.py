"""Quarterly roll and expiry, computed rather than tabulated.

NOT THE ELIGIBILITY RULE. Research eligibility across a contract
boundary is decided in `features.contracts`, from the vendor's
symbology, and nothing in this module is consulted for it. This module
explains WHY the expiry sessions look the way they do, and serves as a
FALSIFIER: if the contract transitions read off the symbology do not
track the quarterly cycle this computes, the instrument ids are not
stable per contract and the eligibility rule is unsound. That check
lives in `scripts/analyze_exclusions.py`.

The distinction matters because a calendar rule is right about a
typical year and wrong about every early, late, staggered or
holiday-shifted roll -- which is precisely the case where a
contaminated observation would slip through.

Unlike holidays, this needs no data table and no coverage limit: CME
equity-index futures expire on the THIRD FRIDAY of March, June,
September and December, and that is a rule, not a list. It is therefore
correct for any year, including ones the holiday table does not cover.

WHY THIS MATTERS TO A CONTINUOUS SERIES

`NQ.c.0` is not an instrument; it is a stitch of successive front-month
contracts. Two consequences, and the second is easy to miss:

  1. ON the expiry session the expiring contract settles to a Special
     Opening Quotation and stops trading at the cash open -- exactly
     when the RTH window begins. A continuous series still pointing at
     it has almost no RTH tape that day.

  2. ACROSS the roll the price level jumps. The new front month trades
     at a different level for carry reasons, and that gap is not a
     market move. Any feature that reaches back across the boundary --
     prior-day high/low, overnight high/low -- is then comparing prices
     from two different contracts and describing a level that never
     existed in the one now trading.

The second is why `is_roll_adjacent` exists. Dropping the expiry session
alone leaves the FOLLOWING session carrying corrupted prior-day levels,
which is precisely what the preregistered sweep hypotheses key off.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, List, Optional, Sequence, Set

QUARTERLY_MONTHS = (3, 6, 9, 12)


def third_friday(year: int, month: int) -> date:
    """The expiry rule itself. No table, so no uncovered years."""
    day = date(year, month, 1)
    offset = (4 - day.weekday()) % 7          # first Friday
    return day + timedelta(days=offset + 14)  # plus two weeks


def is_quarterly_expiry(day: date) -> bool:
    return day.month in QUARTERLY_MONTHS and day == third_friday(day.year, day.month)


def expiries_between(start: date, end: date) -> List[date]:
    out: List[date] = []
    for year in range(start.year, end.year + 1):
        for month in QUARTERLY_MONTHS:
            expiry = third_friday(year, month)
            if start <= expiry <= end:
                out.append(expiry)
    return sorted(out)


def next_session_after(day: date, sessions: Sequence[date]) -> Optional[date]:
    for candidate in sorted(sessions):
        if candidate > day:
            return candidate
    return None


def roll_adjacent_sessions(sessions: Sequence[date]) -> Set[date]:
    """Sessions whose PRIOR-DAY and OVERNIGHT levels reach back across a
    roll boundary, and are therefore levels from a different contract."""
    ordered = sorted(sessions)
    if not ordered:
        return set()
    adjacent: Set[date] = set()
    for expiry in expiries_between(ordered[0], ordered[-1]):
        following = next_session_after(expiry, ordered)
        if following is not None:
            adjacent.add(following)
    return adjacent


def classify_session(day: date, sessions: Sequence[date]) -> str:
    if is_quarterly_expiry(day):
        return "expiry"
    if day in roll_adjacent_sessions(sessions):
        return "roll_adjacent"
    return "ordinary"
