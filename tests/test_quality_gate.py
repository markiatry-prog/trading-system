"""The redesigned data-quality gate.

The original gate excluded 695 of 1,256 real NQ sessions. The arithmetic
showed why: NQ averaged 1,397 bars per session against an assumed 1,380,
so the missing-bar rule could not fire at all, and the exclusions came
from a heuristic comparing each session's LARGEST bar range to its
MEDIAN. On a 23-hour session spanning dead overnight minutes and the RTH
open, that ratio is routinely 20-60x. It was excluding ordinary sessions
and keeping unusually uniform ones -- selection on volatility structure,
which is exactly what must not condition a study.

These tests pin the five-way distinction that replaced it.
"""
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_system.features.calendar import SessionCalendar  # noqa: E402
from trading_system.features.config import FeatureConfig, SessionSpec  # noqa: E402
from trading_system.market_data import Bar, Instrument  # noqa: E402
from trading_system.research.quality import (  # noqa: E402
    DEGRADED_SOURCE_DATES, ExclusionRule, Observation, QualityReport,
    QualityThresholds, assess_day, expected_rth_minutes, implausible_prints,
    longest_missing_run)

CAL = SessionCalendar(SessionSpec())
NQ = Instrument(symbol="NQ.c.0", product="NQ", tick_size=Decimal("0.25"))
DAY = date(2026, 1, 12)          # an ordinary Monday
RTH_OPEN = CAL.rth_open_at(DAY)  # 14:30 UTC in January


def bar_at(minute, price=20000, rng=2, volume=100, origin=RTH_OPEN):
    t = origin + timedelta(minutes=minute)
    p = Decimal(str(price))
    return Bar(instrument=NQ, interval_seconds=60, open=p,
               high=p + Decimal(str(rng)), low=p - Decimal(str(rng)),
               close=p, volume=volume, observed_at=t,
               captured_at=t + timedelta(milliseconds=20), provider="fixture")


def full_rth(minutes=None, **kw):
    minutes = minutes if minutes is not None else expected_rth_minutes(CAL, DAY)
    return [bar_at(i, price=20000 + i * 0.25, **kw) for i in range(minutes)]


def overnight(n=200):
    """Sparse overnight bars, as a trade-driven schema really produces."""
    start = RTH_OPEN - timedelta(hours=10)
    return [bar_at(i * 3, price=19990, origin=start) for i in range(n)]


# --- the regression that motivated the redesign -----------------------

def test_a_normal_session_with_a_huge_range_ratio_now_passes():
    """The exact shape the old gate rejected 695 times: quiet overnight
    minutes plus an active RTH open. Range ratio far above 10, and
    nothing wrong with the data."""
    bars = overnight(200) + full_rth()
    # one RTH bar with a genuinely large range, as at the open
    bars[len(overnight(200)) + 1] = bar_at(1, price=20000, rng=60)
    q = assess_day(DAY, "NQ.c.0", bars, CAL)
    assert q.max_range_ratio > 10, "fixture must reproduce the old trigger"
    assert q.passed, f"excluded by {q.exclusions}"


def test_sparse_overnight_is_observed_never_excluded():
    """A minute with no trade produces no bar. That is silence, not a
    defect, and must not remove the session from the sample."""
    q = assess_day(DAY, "NQ.c.0", overnight(50) + full_rth(), CAL)
    assert q.passed
    assert Observation.SPARSE_OVERNIGHT.value in q.observations
    assert not q.exclusions


def test_no_overnight_bars_at_all_is_observed_not_excluded():
    q = assess_day(DAY, "NQ.c.0", full_rth(), CAL)
    assert q.passed
    assert Observation.NO_OVERNIGHT_BARS.value in q.observations


# --- true feed gaps DO exclude ---------------------------------------

def test_a_contiguous_rth_outage_is_excluded():
    """Minutes missing from the LIQUID session is the real defect."""
    bars = full_rth()
    del bars[100:140]                       # a 40-minute hole at midday
    q = assess_day(DAY, "NQ.c.0", bars, CAL)
    assert not q.passed
    assert ExclusionRule.RTH_CONTIGUOUS_GAP.value in q.exclusions
    assert q.longest_rth_gap >= 40


def test_scattered_single_missing_minutes_are_tolerated():
    """Ordinary thin minutes inside RTH must not exclude a session."""
    bars = [b for i, b in enumerate(full_rth()) if i % 40 != 0]
    q = assess_day(DAY, "NQ.c.0", bars, CAL)
    assert q.longest_rth_gap <= 1
    assert q.passed, q.exclusions


def test_broad_rth_shortfall_is_excluded():
    q = assess_day(DAY, "NQ.c.0", full_rth()[:100], CAL)
    assert not q.passed
    assert ExclusionRule.RTH_COVERAGE.value in q.exclusions


def test_no_data_is_excluded():
    q = assess_day(DAY, "NQ.c.0", [], CAL)
    assert not q.passed and ExclusionRule.NO_DATA.value in q.exclusions


# --- implausible prints ----------------------------------------------

def test_a_reverting_spike_is_flagged_but_a_real_move_is_not():
    """A genuine move carries through to the next bar; a bad print
    spikes and the tape resumes as if nothing happened."""
    clean = full_rth()
    assert not implausible_prints(clean, Decimal("0.05"))

    spiked = full_rth()
    spiked[200] = bar_at(200, price=20050, rng=3000)   # +15% and back
    assert implausible_prints(spiked, Decimal("0.05"))
    q = assess_day(DAY, "NQ.c.0", spiked, CAL)
    assert not q.passed
    assert ExclusionRule.IMPLAUSIBLE_PRINT.value in q.exclusions


def test_a_sustained_large_move_is_not_a_bad_print():
    """A 2% minute that the next bar continues from is a market event."""
    bars = full_rth()                      # the FULL session, not a slice
    for i in range(150, len(bars)):         # a step up that persists
        bars[i] = bar_at(i, price=20400 + (i - 150) * 0.25)
    assert not implausible_prints(bars, Decimal("0.05"))
    assert assess_day(DAY, "NQ.c.0", bars, CAL).passed


# --- calendar-derived expectations ------------------------------------

def test_expected_minutes_come_from_the_calendar_not_a_constant():
    normal = expected_rth_minutes(CAL, date(2026, 12, 23))
    early = expected_rth_minutes(CAL, date(2026, 12, 24))
    assert normal == 390
    assert early == 210, "an early close must shorten the expectation"


def test_an_early_close_session_passes_on_its_own_shorter_window():
    """Judged against 390 minutes a half-day looks half missing. Judged
    against its real window it is complete."""
    early_day = date(2026, 12, 24)
    opened = CAL.rth_open_at(early_day)
    minutes = expected_rth_minutes(CAL, early_day)
    bars = [bar_at(i, price=20000 + i * 0.25, origin=opened) for i in range(minutes)]
    q = assess_day(early_day, "NQ.c.0", bars, CAL)
    assert q.expected_rth_minutes == minutes
    assert q.passed
    assert Observation.EARLY_CLOSE.value in q.observations
    assert Observation.SHORT_SESSION.value in q.observations


def test_dst_changes_the_expected_window_automatically():
    for day in (date(2026, 1, 15), date(2026, 7, 15)):
        assert expected_rth_minutes(CAL, day) == 390


# --- structural ------------------------------------------------------

def test_duplicate_and_out_of_order_timestamps_still_exclude():
    bars = full_rth()
    dup = bars + [bars[10]]
    assert ExclusionRule.DUPLICATE_TIMESTAMPS.value in \
        assess_day(DAY, "NQ.c.0", dup, CAL).exclusions
    swapped = full_rth()
    swapped[5], swapped[6] = swapped[6], swapped[5]
    assert ExclusionRule.OUT_OF_ORDER.value in \
        assess_day(DAY, "NQ.c.0", swapped, CAL).exclusions


def test_degraded_dates_exclude_and_the_list_starts_empty():
    """A speculative list would exclude good data while looking rigorous."""
    assert DEGRADED_SOURCE_DATES == {}
    q = assess_day(DAY, "NQ.c.0", full_rth(), CAL, degraded=frozenset({DAY}))
    assert not q.passed
    assert ExclusionRule.DEGRADED_SOURCE_DATE.value in q.exclusions


def test_longest_missing_run_counts_contiguously():
    assert longest_missing_run([0, 1, 2, 7, 8, 9], 10) == 4
    assert longest_missing_run(list(range(10)), 10) == 0
    assert longest_missing_run([], 5) == 5


# --- reporting --------------------------------------------------------

def test_the_report_separates_exclusions_from_observations():
    report = QualityReport()
    report.add(assess_day(DAY, "NQ.c.0", overnight(30) + full_rth(), CAL))
    report.add(assess_day(DAY, "NQ.c.0", full_rth()[:50], CAL))
    s = report.summary()
    assert s["days_assessed"] == 2 and s["days_passed"] == 1
    assert ExclusionRule.RTH_COVERAGE.value in s["exclusion_reasons"]
    assert Observation.SPARSE_OVERNIGHT.value in s["observations"]


def test_range_ratio_is_reported_but_never_excludes():
    """Kept visible so the change from the old gate stays auditable."""
    bars = full_rth()
    bars[3] = bar_at(3, price=20000, rng=80)
    q = assess_day(DAY, "NQ.c.0", bars, CAL)
    assert q.max_range_ratio > 20
    assert q.passed
    src = (ROOT / "trading_system" / "research" / "quality.py").read_text()
    assert "max_range_ratio" in src
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "max_range_ratio >" not in code, "the ratio must not gate anything"
