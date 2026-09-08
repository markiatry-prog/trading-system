"""Data-quality gating. Runs BEFORE any statistic is computed.

WHAT THE FIRST REAL-DATA RUN TAUGHT

The original gate excluded 695 of 1,256 NQ sessions. That was not a data
problem; it was a gate problem, and the arithmetic says so. NQ averaged
1,397 bars per session against an assumed 1,380, so the missing-bar rule
could not have fired at all. The exclusions came from a "bad print"
heuristic comparing each session's LARGEST bar range to its MEDIAN.

On a 23-hour session that mixes dead overnight minutes with the RTH
open, a max/median range ratio above 10 is the normal state of the
market, not a defect. The rule therefore excluded ordinary sessions and
kept unusually uniform ones -- a selection bias toward sessions whose
activity was evenly spread, which is precisely the wrong thing to
condition a study on.

FIVE THINGS THAT MUST NOT BE CONFUSED

  1. LEGITIMATE NO-TRADE MINUTES. Bar schemas aggregate trades, so a
     minute with no trade produces no bar. Overnight NQ has many. This
     is the market being quiet, and it is recorded, never excluded.
  2. EXPECTED SESSION STRUCTURE. The daily maintenance halt, the Sunday
     open, weekends. Absent by design.
  3. EARLY CLOSES AND HOLIDAYS. A half-day is a short session, not a
     broken one. The expected window comes from the calendar.
  4. TRUE FEED GAPS. Minutes missing from the LIQUID part of the
     session, especially in a contiguous run. This is the real defect
     and the only coverage condition that excludes.
  5. KNOWN DEGRADED DATES. An explicit, versioned list. Empty until
     something is actually known to be bad -- a placeholder full of
     guesses would be worse than none.

The expected minute count is computed per session from the calendar, so
early closes and DST are handled by construction rather than by a fixed
number that is wrong twice a year and on every half-day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from ..features.calendar import SessionCalendar, early_close_time
from ..market_data import Bar


class ExclusionRule(str, Enum):
    """Conditions that EXCLUDE a session. Each is a genuine defect."""
    NO_DATA = "no_data"
    RTH_COVERAGE = "rth_coverage"
    RTH_CONTIGUOUS_GAP = "rth_contiguous_gap"
    IMPLAUSIBLE_PRINT = "implausible_print"
    DUPLICATE_TIMESTAMPS = "duplicate_timestamps"
    OUT_OF_ORDER = "out_of_order"
    DEGRADED_SOURCE_DATE = "degraded_source_date"


class Observation(str, Enum):
    """Recorded and reported, but NEVER excluding. These describe the
    market or the calendar, not the data's integrity."""
    SPARSE_OVERNIGHT = "sparse_overnight"
    EARLY_CLOSE = "early_close"
    SHORT_SESSION = "short_session"
    NO_OVERNIGHT_BARS = "no_overnight_bars"


@dataclass(frozen=True)
class QualityThresholds:
    # Fraction of RTH minutes that must carry a bar. RTH in NQ/ES trades
    # essentially every minute, so a real shortfall here is a feed gap.
    min_rth_coverage: Decimal = Decimal("0.95")
    # A contiguous run of missing RTH minutes. Scattered singles are
    # ordinary; a run is an outage.
    max_rth_gap_minutes: int = 10
    # A one-minute excursion beyond BOTH neighbours by this fraction of
    # price, which then reverts. A genuine move carries through to the
    # next bar; a bad print does not. 5% in one minute on an equity
    # index future is not a market move.
    implausible_excursion: Decimal = Decimal("0.05")
    min_rth_bars: int = 30


# Dates known to be degraded at source. Deliberately EMPTY: entries are
# added only when a specific date is established to be bad, with the
# reason recorded. A speculative list would exclude good data and look
# rigorous while doing it.
DEGRADED_SOURCE_DATES: Dict[str, FrozenSet[date]] = {}


@dataclass
class DayQuality:
    session_date: date
    instrument_symbol: str
    passed: bool
    exclusions: List[str] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)
    bar_count: int = 0
    rth_bar_count: int = 0
    overnight_bar_count: int = 0
    expected_rth_minutes: int = 0
    rth_coverage: float = 0.0
    longest_rth_gap: int = 0
    total_volume: int = 0

    # kept for reporting, never for excluding
    max_range_ratio: float = 0.0

    @property
    def reasons(self) -> List[str]:
        """Backwards-compatible alias: the exclusions, in reason form."""
        return self.exclusions

    def as_row(self) -> dict:
        return {
            "session_date": self.session_date.isoformat(),
            "instrument_symbol": self.instrument_symbol,
            "passed": self.passed,
            "exclusions": sorted(self.exclusions),
            "observations": sorted(self.observations),
            "bar_count": self.bar_count,
            "rth_bar_count": self.rth_bar_count,
            "overnight_bar_count": self.overnight_bar_count,
            "expected_rth_minutes": self.expected_rth_minutes,
            "rth_coverage": round(self.rth_coverage, 4),
            "longest_rth_gap": self.longest_rth_gap,
            "total_volume": self.total_volume,
            "max_range_ratio": round(self.max_range_ratio, 2),
        }


def expected_rth_minutes(calendar: SessionCalendar, session_date: date) -> int:
    """From the calendar, so early closes and DST are handled by
    construction rather than by a constant that is wrong on half-days."""
    opened = calendar.rth_open_at(session_date)
    closed = calendar.rth_close_at(session_date)
    return max(0, int((closed - opened).total_seconds() // 60))


def _minute_index(instant: datetime, origin: datetime) -> int:
    return int((instant - origin).total_seconds() // 60)


def longest_missing_run(present: Sequence[int], expected: int) -> int:
    """Longest contiguous run of absent minutes inside the window."""
    if expected <= 0:
        return 0
    have = set(present)
    longest = run = 0
    for minute in range(expected):
        if minute in have:
            run = 0
        else:
            run += 1
            longest = max(longest, run)
    return longest


def implausible_prints(bars: Sequence[Bar], tolerance: Decimal) -> List[Bar]:
    """Bars whose extreme is contradicted by BOTH neighbours.

    A genuine move carries through: the next bar opens near where the
    move went. A bad print spikes and the tape resumes as if it never
    happened. Comparing against neighbours rather than against the
    session's median range is what makes this robust on a session that
    legitimately contains both dead overnight minutes and the open.
    """
    flagged: List[Bar] = []
    for i in range(1, len(bars) - 1):
        bar, prev, nxt = bars[i], bars[i - 1], bars[i + 1]
        anchor_high = max(prev.close, nxt.open)
        anchor_low = min(prev.close, nxt.open)
        if anchor_high <= 0:
            continue
        up = (bar.high - anchor_high) / anchor_high
        down = (anchor_low - bar.low) / anchor_high if anchor_low > 0 else Decimal(0)
        if up > tolerance or down > tolerance:
            flagged.append(bar)
    return flagged


def assess_day(session_date: date, instrument_symbol: str, bars: Sequence[Bar],
               calendar: SessionCalendar,
               thresholds: QualityThresholds = QualityThresholds(),
               degraded: Optional[FrozenSet[date]] = None) -> DayQuality:
    """Classify one session. Exclusions are defects; observations are not."""
    q = DayQuality(session_date=session_date, instrument_symbol=instrument_symbol,
                   passed=True, bar_count=len(bars))

    degraded = degraded if degraded is not None else DEGRADED_SOURCE_DATES.get(
        instrument_symbol, frozenset())
    if session_date in degraded:
        q.exclusions.append(ExclusionRule.DEGRADED_SOURCE_DATE.value)
        q.passed = False
        return q

    if not bars:
        q.exclusions.append(ExclusionRule.NO_DATA.value)
        q.passed = False
        return q

    q.total_volume = sum(b.volume for b in bars)

    times = [b.observed_at for b in bars]
    if len(set(times)) != len(times):
        q.exclusions.append(ExclusionRule.DUPLICATE_TIMESTAMPS.value)
    if times != sorted(times):
        q.exclusions.append(ExclusionRule.OUT_OF_ORDER.value)

    opened = calendar.rth_open_at(session_date)
    closed = calendar.rth_close_at(session_date)
    q.expected_rth_minutes = expected_rth_minutes(calendar, session_date)
    if early_close_time(session_date) is not None:
        q.observations.append(Observation.EARLY_CLOSE.value)
    if q.expected_rth_minutes < 390:
        q.observations.append(Observation.SHORT_SESSION.value)

    rth = [b for b in bars if opened <= b.observed_at < closed]
    overnight = [b for b in bars if not (opened <= b.observed_at < closed)]
    q.rth_bar_count = len(rth)
    q.overnight_bar_count = len(overnight)

    # 4. TRUE FEED GAPS -- the only coverage condition that excludes.
    if q.expected_rth_minutes:
        minutes = [_minute_index(b.observed_at, opened) for b in rth]
        q.rth_coverage = len(set(minutes)) / q.expected_rth_minutes
        q.longest_rth_gap = longest_missing_run(minutes, q.expected_rth_minutes)
        if Decimal(str(q.rth_coverage)) < thresholds.min_rth_coverage:
            q.exclusions.append(ExclusionRule.RTH_COVERAGE.value)
        if q.longest_rth_gap > thresholds.max_rth_gap_minutes:
            q.exclusions.append(ExclusionRule.RTH_CONTIGUOUS_GAP.value)
    if len(rth) < thresholds.min_rth_bars:
        if ExclusionRule.RTH_COVERAGE.value not in q.exclusions:
            q.exclusions.append(ExclusionRule.RTH_COVERAGE.value)

    # 1 and 2. Overnight sparsity and structural gaps: OBSERVED, not judged.
    if not overnight:
        q.observations.append(Observation.NO_OVERNIGHT_BARS.value)
    elif q.expected_rth_minutes and len(overnight) < q.expected_rth_minutes:
        q.observations.append(Observation.SPARSE_OVERNIGHT.value)

    bad = implausible_prints(bars, thresholds.implausible_excursion)
    if bad:
        q.exclusions.append(ExclusionRule.IMPLAUSIBLE_PRINT.value)

    # Reported for diagnostics only. This ratio is what the old gate
    # excluded on; it is kept visible so the change is auditable.
    ranges = sorted((b.high - b.low) for b in bars)
    median = ranges[len(ranges) // 2]
    if median > 0:
        q.max_range_ratio = float(ranges[-1] / median)

    q.passed = not q.exclusions
    return q


@dataclass
class QualityReport:
    """The denominator, stated honestly."""
    assessed: List[DayQuality] = field(default_factory=list)

    def add(self, day: DayQuality) -> None:
        self.assessed.append(day)

    @property
    def passed_days(self) -> List[date]:
        return sorted(d.session_date for d in self.assessed if d.passed)

    @property
    def excluded_days(self) -> List[DayQuality]:
        return [d for d in self.assessed if not d.passed]

    def summary(self) -> dict:
        by_rule: Dict[str, int] = {}
        for d in self.excluded_days:
            for rule in d.exclusions:
                by_rule[rule] = by_rule.get(rule, 0) + 1
        observed: Dict[str, int] = {}
        for d in self.assessed:
            for obs in d.observations:
                observed[obs] = observed.get(obs, 0) + 1
        return {
            "days_assessed": len(self.assessed),
            "days_passed": len(self.passed_days),
            "days_excluded": len(self.excluded_days),
            "exclusion_reasons": dict(sorted(by_rule.items())),
            "observations": dict(sorted(observed.items())),
        }
