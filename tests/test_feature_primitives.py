"""Each primitive must demonstrably FIRE on a constructed case, and stay
silent on a constructed near-miss.

A detector that never fires is indistinguishable from no detector, and
one that always fires is noise. Both failure modes are silent in
aggregate statistics, so each primitive is pinned from both sides.
"""
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures_market import NQ, RTH_OPEN, bar, flat_series  # noqa: E402
from trading_system.features.config import FeatureConfig  # noqa: E402
from trading_system.features.engine import FeatureEngine  # noqa: E402
from trading_system.features.records import EventType, FeatureType  # noqa: E402


def types_of(records):
    return [r.type for r in records]


def _two_sessions(day1_bars, day2_bars):
    return day1_bars + day2_bars


# --- liquidity sweeps -------------------------------------------------

def _sweep_setup(day2_high, day2_close):
    """Day 1 establishes a prior-day high; day 2 pokes through it."""
    day1 = [bar(i, 20000 + i, 20005 + i, 19995 + i, 20001 + i) for i in range(5)]
    origin = RTH_OPEN + timedelta(days=1)
    day2 = [bar(0, 20000, 20002, 19998, 20001, origin=origin),
            bar(1, 20001, day2_high, 19999, day2_close, origin=origin)]
    return day1 + day2


def test_liquidity_sweep_fires_when_the_level_is_pierced_and_reclaimed():
    # Day 1 RTH high is 20009 (last bar: 20005+4).
    records = FeatureEngine(NQ).run(_sweep_setup(day2_high=20012, day2_close=20005))
    assert EventType.LIQUIDITY_SWEEP_HIGH.value in types_of(records)


def test_no_sweep_when_price_closes_beyond_the_level():
    """Closing through it is a break, not a sweep. The close-back is the
    whole distinction, and conflating them would merge two populations
    that later research needs to separate."""
    records = FeatureEngine(NQ).run(_sweep_setup(day2_high=20012, day2_close=20011))
    assert EventType.LIQUIDITY_SWEEP_HIGH.value not in types_of(records)


def test_no_sweep_when_the_wick_does_not_reach_the_level():
    records = FeatureEngine(NQ).run(_sweep_setup(day2_high=20006, day2_close=20002))
    assert EventType.LIQUIDITY_SWEEP_HIGH.value not in types_of(records)


# --- displacement -----------------------------------------------------

def test_displacement_fires_on_an_outsized_bar_and_not_on_ordinary_ones():
    quiet = [bar(i, 20000 + i, 20001 + i, 19999 + i, 20000 + i) for i in range(20)]
    records = FeatureEngine(NQ).run(quiet)
    assert EventType.DISPLACEMENT_UP.value not in types_of(records)
    assert EventType.DISPLACEMENT_DOWN.value not in types_of(records)

    big = quiet + [bar(20, 20020, 20080, 20018, 20075)]
    records = FeatureEngine(NQ).run(big)
    assert EventType.DISPLACEMENT_UP.value in types_of(records)


def test_displacement_threshold_is_configurable_and_bites():
    quiet = [bar(i, 20000 + i, 20002 + i, 19998 + i, 20000 + i) for i in range(20)]
    bars = quiet + [bar(20, 20020, 20030, 20018, 20028)]
    loose = FeatureEngine(NQ, FeatureConfig(displacement_atr_multiple=Decimal("1.2"))).run(bars)
    tight = FeatureEngine(NQ, FeatureConfig(displacement_atr_multiple=Decimal("10"))).run(bars)
    assert EventType.DISPLACEMENT_UP.value in types_of(loose)
    assert EventType.DISPLACEMENT_UP.value not in types_of(tight)


# --- fair value gaps --------------------------------------------------

def _fvg_up_bars():
    return [bar(0, 20000, 20002, 19998, 20001),
            bar(1, 20001, 20030, 20000, 20028),
            bar(2, 20028, 20040, 20020, 20035)]


def test_fvg_forms_and_then_fills_and_then_inverts():
    bars = _fvg_up_bars() + [
        bar(3, 20035, 20036, 20010, 20015),   # trades back into the gap
        bar(4, 20015, 20016, 19990, 19995),   # closes below it -> inverted
    ]
    records = FeatureEngine(NQ).run(bars)
    seen = types_of(records)
    assert EventType.FVG_FORMED_UP.value in seen
    assert EventType.FVG_FILLED.value in seen
    assert EventType.FVG_INVERTED.value in seen


def test_minimum_gap_size_suppresses_noise():
    """A one-tick gap on every other bar would drown any statistic."""
    tiny = [bar(0, 20000, 20002.00, 19998, 20001),
            bar(1, 20001, 20010.00, 20000, 20008),
            bar(2, 20008, 20012.00, 20002.25, 20010)]   # 0.25 gap only
    strict = FeatureEngine(NQ, FeatureConfig(fvg_min_ticks=4)).run(tiny)
    loose = FeatureEngine(NQ, FeatureConfig(fvg_min_ticks=1)).run(tiny)
    assert EventType.FVG_FORMED_UP.value not in types_of(strict)
    assert EventType.FVG_FORMED_UP.value in types_of(loose)


# --- structure --------------------------------------------------------

def test_structure_break_requires_a_confirmed_swing_first():
    """No pivot, no break. A break measured against an unconfirmed level
    would be using information that was not yet available."""
    rising = [bar(i, 20000 + i * 5, 20003 + i * 5, 19999 + i * 5, 20002 + i * 5)
              for i in range(6)]
    records = FeatureEngine(NQ).run(rising)
    swings = [r for r in records if r.type == FeatureType.SWING_HIGH.value]
    breaks = [r for r in records if r.type == EventType.STRUCTURE_BREAK_UP.value]
    assert not swings and not breaks


# --- location features ------------------------------------------------

def test_distance_features_are_signed_and_relative_to_close():
    bars = flat_series(40)
    records = FeatureEngine(NQ, FeatureConfig(opening_range_minutes=5)).run(bars)
    dists = [r for r in records
             if r.type == FeatureType.DISTANCE_TO_OPENING_RANGE_HIGH.value]
    assert dists
    last = dists[-1]
    orh = [r for r in records
           if r.type == FeatureType.OPENING_RANGE_HIGH.value][-1]
    assert last.value == bars[-1].close - orh.value


def test_above_below_vwap_is_a_state_not_a_number():
    records = FeatureEngine(NQ).run(flat_series(30))
    states = {r.state for r in records if r.type == FeatureType.ABOVE_VWAP.value}
    assert states and states <= {"above", "below"}


def test_vwap_is_computed_from_rth_bars_only():
    """Including the thin overnight tape produces a level that matches no
    charting package a human would compare against."""
    overnight = [bar(i, 19000, 19001, 18999, 19000, volume=1000,
                     origin=RTH_OPEN - timedelta(hours=3)) for i in range(3)]
    rth = flat_series(5)
    records = FeatureEngine(NQ).run(overnight + rth)
    vwaps = [r.value for r in records if r.type == FeatureType.VWAP.value]
    assert vwaps, "no VWAP emitted"
    assert all(v > Decimal("19500") for v in vwaps), (
        "VWAP was dragged toward the overnight price, so overnight bars "
        "leaked into the RTH calculation"
    )
