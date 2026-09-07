"""Lookahead leakage tests.

The ticket calls this security-equivalent, and it is: leakage does not
produce an error, it produces excellent results. A backtest built on a
leaked feature looks like an edge and is worth nothing, and by the time
that is discovered the strategy has been trusted for months.

The central test is the prefix-invariance property below. It is stronger
than inspecting the code, because it holds the engine to the only
definition that matters: what it emitted from a truncated history must
be exactly what it emitted from the full history.
"""
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures_market import NQ, RTH_OPEN, bar, flat_series, series_with_swing_high  # noqa: E402
from trading_system.features.config import FeatureConfig  # noqa: E402
from trading_system.features.engine import FeatureEngine  # noqa: E402
from trading_system.features.records import EventType, FeatureType, as_of  # noqa: E402


def _rows(records):
    return [r.as_row() for r in records]


def test_prefix_invariance_is_the_lookahead_proof():
    """Running on the first N bars must produce EXACTLY what running on
    the whole series produced up to that point.

    If any computation consulted a later bar, the full run and the
    truncated run would disagree. Nothing else about the engine needs to
    be trusted for this to be conclusive.
    """
    bars = flat_series(40)
    full = FeatureEngine(NQ).run(bars)

    for cut in (5, 12, 25, 39):
        partial = FeatureEngine(NQ).run(bars[:cut])
        cutoff = bars[cut - 1].closed_at
        expected = [r for r in full if r.available_at <= cutoff]
        assert _rows(partial) == _rows(expected), (
            f"prefix of {cut} bars disagrees with the full run; "
            f"something consulted a future bar"
        )


def test_swing_high_is_dated_honestly_and_published_late():
    """The hard case. A pivot cannot be known when it happens."""
    bars = series_with_swing_high(peak_at=5, n=12)
    records = FeatureEngine(NQ, FeatureConfig(swing_lookback_bars=2)).run(bars)
    swings = [r for r in records if r.type == FeatureType.SWING_HIGH.value]
    assert swings, "no swing high detected in a series built around one"
    swing = swings[0]
    assert swing.effective_at == bars[5].observed_at
    assert swing.available_at == bars[7].closed_at
    # k+1 intervals, not k: the k bars after the pivot must each COMPLETE,
    # and the pivot itself is dated at its bar's open (matching
    # Bar.observed_at). k=2 on 1-minute bars therefore costs 3 minutes of
    # latency, which is the number a live system would actually pay.
    assert swing.confirmation_lag == timedelta(minutes=3)


def test_a_swing_is_not_visible_before_its_confirming_bar():
    bars = series_with_swing_high(peak_at=5, n=12)
    records = FeatureEngine(NQ, FeatureConfig(swing_lookback_bars=2)).run(bars)
    swing = [r for r in records if r.type == FeatureType.SWING_HIGH.value][0]
    just_before = swing.available_at - timedelta(seconds=1)
    assert swing not in as_of(records, just_before)
    assert swing in as_of(records, swing.available_at)


def test_larger_swing_lookback_costs_more_confirmation_delay():
    """Not a sensitivity knob: k is a latency choice, and the research
    system must be able to see that."""
    bars = series_with_swing_high(peak_at=6, n=16)
    lags = {}
    for k in (2, 3):
        records = FeatureEngine(NQ, FeatureConfig(swing_lookback_bars=k)).run(bars)
        swings = [r for r in records if r.type == FeatureType.SWING_HIGH.value]
        if swings:
            lags[k] = swings[0].confirmation_lag
    assert lags.get(3, timedelta(0)) > lags.get(2, timedelta(0))


def test_fvg_effective_time_precedes_its_availability():
    """The gap is defined by the middle bar but knowable only at the third."""
    bars = [
        bar(0, 20000, 20002, 19998, 20001),
        bar(1, 20001, 20030, 20000, 20028),
        bar(2, 20028, 20040, 20020, 20035),   # low 20020 > bar0 high 20002
    ]
    records = FeatureEngine(NQ).run(bars)
    gaps = [r for r in records if r.type == EventType.FVG_FORMED_UP.value]
    assert gaps, "no FVG detected in a constructed gap"
    gap = gaps[0]
    assert gap.effective_at == bars[1].observed_at
    assert gap.available_at == bars[2].closed_at
    assert gap.available_at > gap.effective_at


def test_no_record_is_ever_available_before_it_is_effective():
    """Enforced in the type, asserted here across a whole run."""
    records = FeatureEngine(NQ).run(flat_series(60))
    assert records
    for r in records:
        assert r.available_at >= r.effective_at


def test_everything_becomes_available_exactly_on_a_bar_close():
    """Nothing may become knowable mid-bar.

    Stating this as "never at a bar OPEN time" does not work and the
    attempt is instructive: with contiguous bars, bar N's close IS bar
    N+1's open, so that assertion is unsatisfiable by construction. The
    property that actually matters is that every availability timestamp
    lands on some bar boundary -- never inside an interval, which would
    mean using information from a bar that had not finished forming.
    """
    bars = flat_series(20)
    records = FeatureEngine(NQ).run(bars)
    closes = {b.closed_at for b in bars}
    assert records
    for r in records:
        assert r.available_at in closes, (
            f"{r.type} becomes available at {r.available_at.isoformat()}, "
            f"which is not a bar boundary"
        )
