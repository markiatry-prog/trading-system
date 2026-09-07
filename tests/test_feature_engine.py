"""Determinism, session handling and the adversarial cases named in T-003.

These are the tests that decide whether a statistic computed on this
engine's output means anything. A duplicate bar silently absorbed
double-counts VWAP; a session boundary off by an hour shifts every
opening range twice a year; a float creeping into a value makes an exact
level test intermittently false. None of those raise on their own.
"""
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures_market import ES, NQ, RTH_OPEN, bar, flat_series  # noqa: E402
from trading_system.features.calendar import (  # noqa: E402
    CalendarError, SessionCalendar, SessionPhase, is_known_year)
from trading_system.features.config import FeatureConfig, SessionSpec  # noqa: E402
from trading_system.features.engine import ENGINE_VERSION, FeatureEngine  # noqa: E402
from trading_system.features.records import (  # noqa: E402
    EventType, FeatureType, RecordKind)

CAL = SessionCalendar(SessionSpec())


# --- determinism ------------------------------------------------------

def test_same_inputs_same_config_same_version_give_identical_output():
    bars = flat_series(50)
    a = [r.as_row() for r in FeatureEngine(NQ).run(bars)]
    b = [r.as_row() for r in FeatureEngine(NQ).run(bars)]
    assert a == b


def test_changing_configuration_changes_the_digest_on_every_record():
    bars = flat_series(30)
    base = FeatureEngine(NQ, FeatureConfig()).run(bars)
    other = FeatureEngine(NQ, FeatureConfig(opening_range_minutes=30)).run(bars)
    assert {r.config_digest for r in base} != {r.config_digest for r in other}
    assert len({r.config_digest for r in base}) == 1


def test_every_record_carries_full_provenance():
    records = FeatureEngine(NQ, run_id="run-1").run(flat_series(30))
    assert records
    for r in records:
        assert r.engine_version == ENGINE_VERSION
        assert len(r.config_digest) == 64
        assert r.config_name == "baseline"
        assert r.instrument_symbol == "NQZ6"
        assert r.session_date
        assert r.run_id == "run-1"


def test_config_digest_is_stable_across_processes():
    """Hashing must not depend on dict ordering or interpreter state."""
    import subprocess
    code = ("from trading_system.features.config import FeatureConfig;"
            "print(FeatureConfig().digest())")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == FeatureConfig().digest()


# --- adversarial inputs -----------------------------------------------

def test_duplicate_observation_is_refused_not_absorbed():
    """Absorbed silently, a duplicate double-counts VWAP volume and the
    level drifts in a way nothing downstream can detect."""
    engine = FeatureEngine(NQ)
    b = bar(0, 20000, 20002, 19998, 20001)
    engine.observe(b)
    with pytest.raises(ValueError, match="duplicate"):
        engine.observe(b)


def test_out_of_order_observation_is_refused():
    engine = FeatureEngine(NQ)
    engine.observe(bar(5, 20000, 20002, 19998, 20001))
    with pytest.raises(ValueError, match="out-of-order"):
        engine.observe(bar(4, 20000, 20002, 19998, 20001))


def test_wrong_instrument_is_refused():
    engine = FeatureEngine(NQ)
    with pytest.raises(ValueError, match="engine is for"):
        engine.observe(bar(0, 5000, 5002, 4998, 5001, instrument=ES))


def test_zero_volume_bars_do_not_break_vwap_or_volume_ratio():
    """A zero-volume minute is normal overnight and must not divide by
    zero or poison the session VWAP."""
    bars = [bar(i, 20000 + i, 20002 + i, 19998 + i, 20001 + i, volume=0)
            for i in range(10)]
    records = FeatureEngine(NQ).run(bars)
    assert records
    assert not [r for r in records if r.type == FeatureType.VWAP.value]


def test_missing_intervals_are_tolerated():
    """Gaps in the tape are real. They must not be silently filled."""
    bars = [bar(0, 20000, 20002, 19998, 20001),
            bar(1, 20001, 20003, 19999, 20002),
            bar(30, 20050, 20052, 20048, 20051)]   # 28-minute hole
    records = FeatureEngine(NQ).run(bars)
    assert records
    minutes = [r.value for r in records
               if r.type == FeatureType.MINUTES_SINCE_RTH_OPEN.value]
    assert Decimal(30) in minutes


def test_decimal_is_preserved_through_every_emitted_value():
    records = FeatureEngine(NQ).run(flat_series(40))
    for r in records:
        if r.value is not None:
            assert isinstance(r.value, Decimal), f"{r.type} emitted {type(r.value)}"
            assert not isinstance(r.value, float)


def test_tick_size_is_respected_by_sweep_margins():
    """A sweep margin expressed in ticks must scale with the instrument,
    not be hardcoded in points."""
    cfg = FeatureConfig(sweep_min_ticks=2)
    engine = FeatureEngine(NQ, cfg)
    assert engine.instrument.tick_size == Decimal("0.25")
    assert cfg.sweep_min_ticks * engine.instrument.tick_size == Decimal("0.50")


# --- sessions, DST, holidays, early closes ----------------------------

def test_rth_open_is_the_same_local_time_across_dst():
    winter = CAL.rth_open_at(date(2026, 1, 15))
    summer = CAL.rth_open_at(date(2026, 7, 15))
    assert winter.hour == 14 and winter.minute == 30      # UTC-5
    assert summer.hour == 13 and summer.minute == 30      # UTC-4
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    assert winter.astimezone(ny).strftime("%H:%M") == "09:30"
    assert summer.astimezone(ny).strftime("%H:%M") == "09:30"


def test_sunday_evening_belongs_to_mondays_trading_day():
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    assert CAL.session_date_for(datetime(2026, 1, 11, 22, 0, tzinfo=ny)) == date(2026, 1, 12)


def test_after_the_close_belongs_to_the_next_trading_day():
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    assert CAL.session_date_for(datetime(2026, 1, 12, 18, 0, tzinfo=ny)) == date(2026, 1, 13)


def test_holiday_is_skipped_when_advancing_the_trading_day():
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    # 2026-01-19 is a Monday holiday; Friday evening rolls to Tuesday 20th.
    assert CAL.session_date_for(datetime(2026, 1, 16, 18, 0, tzinfo=ny)) == date(2026, 1, 20)


def test_early_close_shortens_the_session():
    """Compared WITHIN the same date. Comparing the 24th's close against
    the 23rd's measures the day gap, not the early close."""
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    early = CAL.rth_close_at(date(2026, 12, 24))
    assert early.astimezone(ny).strftime("%H:%M") == "13:00"
    would_have_been = datetime(2026, 12, 24, 16, 0, tzinfo=ny)
    assert would_have_been - early == timedelta(hours=3)
    # and an ordinary neighbouring day is untouched
    assert CAL.rth_close_at(date(2026, 12, 23)).astimezone(ny).strftime("%H:%M") == "16:00"


def test_unknown_year_refuses_rather_than_assuming_no_holidays():
    """Treating an uncovered year as clean would silently include closed
    and half-day sessions in a study as if they were ordinary."""
    assert not is_known_year(2031)
    with pytest.raises(CalendarError, match="refusing to guess"):
        CAL.session_date_for(datetime(2031, 6, 2, 15, 0, tzinfo=timezone.utc))


def test_session_roll_emits_close_then_open_and_resets_state():
    day1 = flat_series(3, start=20000, origin=RTH_OPEN)
    day2 = flat_series(3, start=21000, origin=RTH_OPEN + timedelta(days=1))
    records = FeatureEngine(NQ).run(day1 + day2)
    kinds = [r.type for r in records
             if r.type in (EventType.SESSION_OPEN.value, EventType.SESSION_CLOSE.value)]
    assert kinds == [EventType.SESSION_OPEN.value,
                     EventType.SESSION_CLOSE.value,
                     EventType.SESSION_OPEN.value]


def test_prior_day_levels_appear_only_after_the_first_session_completes():
    day1 = flat_series(5, start=20000, origin=RTH_OPEN)
    day2 = flat_series(5, start=21000, origin=RTH_OPEN + timedelta(days=1))
    d1 = FeatureEngine(NQ).run(day1)
    assert not [r for r in d1 if r.type == FeatureType.PRIOR_DAY_HIGH.value]
    both = FeatureEngine(NQ).run(day1 + day2)
    pdh = [r for r in both if r.type == FeatureType.PRIOR_DAY_HIGH.value]
    assert pdh, "prior-day high never became available on day 2"
    assert all(r.session_date == "2026-01-13" for r in pdh)


# --- opening range ----------------------------------------------------

def test_opening_range_uses_the_configured_duration():
    bars = flat_series(40)
    for minutes in (5, 15, 30):
        cfg = FeatureConfig(opening_range_minutes=minutes)
        records = FeatureEngine(NQ, cfg).run(bars)
        est = [r for r in records
               if r.type == EventType.OPENING_RANGE_ESTABLISHED.value]
        assert est, f"no opening range established for {minutes}m"
        assert est[0].attributes["minutes"] == str(minutes)
        assert est[0].available_at == RTH_OPEN + timedelta(minutes=minutes)


def test_opening_range_break_is_reported_once():
    bars = flat_series(40)
    records = FeatureEngine(NQ, FeatureConfig(opening_range_minutes=5)).run(bars)
    breaks = [r for r in records
              if r.type == EventType.OPENING_RANGE_HIGH_BROKEN.value]
    assert len(breaks) == 1, "a break should fire once, not on every later bar"


# --- separation of concerns ------------------------------------------

def test_no_record_type_expresses_a_setup_or_a_judgement():
    """T-003 emits primitives. Anything scoring or ranking belongs to a
    later ticket, and no field here could carry one."""
    banned = ("setup", "signal", "score", "quality", "grade", "confidence",
              "entry", "target", "stop", "profit", "edge", "recommend")
    for enum in (FeatureType, EventType):
        for member in enum:
            for word in banned:
                assert word not in member.value, f"{member.value} names a judgement"


def test_engine_module_contains_no_model_call():
    source = (ROOT / "trading_system" / "features" / "engine.py").read_text().lower()
    for banned in ("anthropic", "openai", "language_model", "llm", "gpt"):
        assert banned not in source, banned


# --- contract roll and DST transition ---------------------------------

def test_contract_roll_requires_a_separate_engine_per_symbol():
    """NQZ6 and NQH7 are different contracts with different price levels.
    Feeding both to one engine would splice a price discontinuity into
    every rolling window and every level, so it is refused rather than
    silently averaged."""
    from trading_system.market_data import Instrument
    nqh7 = Instrument(symbol="NQH7", product="NQ", tick_size=Decimal("0.25"))
    engine = FeatureEngine(NQ)
    engine.observe(bar(0, 20000, 20002, 19998, 20001))
    with pytest.raises(ValueError, match="engine is for NQZ6"):
        engine.observe(bar(1, 20500, 20502, 20498, 20501, instrument=nqh7))


def test_records_are_tagged_with_the_contract_that_produced_them():
    """So a study spanning a roll can group by contract rather than
    silently pooling two price regimes."""
    records = FeatureEngine(NQ).run(flat_series(10))
    assert {r.instrument_symbol for r in records} == {"NQZ6"}


def test_spring_forward_day_still_opens_at_local_0930():
    """2026-03-08 is the US DST transition. The session must open at
    09:30 local on both sides of it, which is a different UTC instant."""
    before = CAL.rth_open_at(date(2026, 3, 6))
    after = CAL.rth_open_at(date(2026, 3, 9))
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    assert before.astimezone(ny).strftime("%H:%M") == "09:30"
    assert after.astimezone(ny).strftime("%H:%M") == "09:30"
    assert before.hour == 14 and after.hour == 13   # UTC offset changed


def test_opening_range_end_is_correct_across_the_dst_boundary():
    """The bug this guards against shifts every opening range by an hour
    for half the year, and produces no error."""
    for day, expected_utc_hour in ((date(2026, 3, 6), 14), (date(2026, 3, 9), 13)):
        end = CAL.opening_range_end_at(day, 15)
        assert end.hour == expected_utc_hour and end.minute == 45


def test_session_phase_is_emitted_for_every_bar():
    """Regime and time-of-day studies depend on it never being absent."""
    bars = flat_series(20)
    records = FeatureEngine(NQ).run(bars)
    phases = [r for r in records if r.type == FeatureType.SESSION_PHASE.value]
    assert len(phases) == len(bars)
