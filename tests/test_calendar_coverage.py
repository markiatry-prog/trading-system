"""Holiday and early-close coverage for the full T-004 dataset period.

The first real-data run refused at 2021 because the table stopped at
2024. That refusal was correct behaviour, and these tests exist so the
extension is verified rather than assumed -- a mis-transcribed holiday
would not raise, it would silently include a closed session in a study
and quietly bias every statistic computed from it.
"""
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_system.features.calendar import (  # noqa: E402
    COVERED_YEARS, EARLY_CLOSES, HOLIDAYS, CalendarError, SessionCalendar,
    early_close_time, is_holiday, is_known_year)
from trading_system.features.config import FeatureConfig, SessionSpec  # noqa: E402

CAL = SessionCalendar(SessionSpec())
NY = ZoneInfo("America/New_York")
DATASET_START = date(2021, 9, 1)
DATASET_END = date(2026, 9, 1)


# --- coverage ---------------------------------------------------------

def test_the_table_covers_the_whole_approved_dataset_period():
    for year in range(DATASET_START.year, DATASET_END.year + 1):
        assert is_known_year(year), f"{year} is inside the dataset and uncovered"


def test_every_day_of_the_dataset_period_resolves_without_raising():
    """The end-to-end proof: walk every calendar day the acquisition
    spans and classify it. This is the exact failure the real run hit."""
    day = DATASET_START
    while day <= DATASET_END:
        CAL.session_date_for(datetime(day.year, day.month, day.day, 15, 0, tzinfo=NY))
        day += timedelta(days=1)


def test_covered_years_are_exactly_2021_through_2026():
    assert COVERED_YEARS == (2021, 2022, 2023, 2024, 2025, 2026)


# --- fail-closed preserved -------------------------------------------

@pytest.mark.parametrize("year", [2019, 2020, 2027, 2030])
def test_uncovered_years_still_refuse_to_guess(year):
    """Extending coverage must not soften the guard. Treating an unknown
    year as holiday-free is the failure mode the guard exists for."""
    assert not is_known_year(year)
    with pytest.raises(CalendarError, match="refusing to guess"):
        is_holiday(date(year, 6, 15))


def test_the_guard_message_still_tells_you_what_to_do():
    with pytest.raises(CalendarError, match="Add the year's holidays"):
        is_holiday(date(2027, 6, 15))


# --- structural invariants -------------------------------------------

def test_no_listed_holiday_falls_on_a_weekend():
    """A weekend date in the table means a transcription error: weekends
    are already non-trading, so listing one is meaningless and signals
    the wrong date was written."""
    for year, days in HOLIDAYS.items():
        for d in days:
            assert d.weekday() < 5, f"{d} ({year}) is a {d.strftime('%A')}"
            assert d.year == year, f"{d} filed under {year}"


def test_no_early_close_is_also_a_full_holiday():
    for year, closes in EARLY_CLOSES.items():
        for d in closes:
            assert d.weekday() < 5, f"{d} is a weekend"
            assert d not in HOLIDAYS[year], f"{d} is both a holiday and an early close"


def test_every_year_has_a_plausible_holiday_count():
    for year, days in HOLIDAYS.items():
        assert 9 <= len(days) <= 10, f"{year} has {len(days)} holidays"


# --- named holidays, per year ----------------------------------------

NEW_YEAR = {2021: date(2021, 1, 1), 2023: date(2023, 1, 2),
            2024: date(2024, 1, 1), 2025: date(2025, 1, 1),
            2026: date(2026, 1, 1)}
GOOD_FRIDAY = {2021: date(2021, 4, 2), 2022: date(2022, 4, 15),
               2023: date(2023, 4, 7), 2024: date(2024, 3, 29),
               2025: date(2025, 4, 18), 2026: date(2026, 4, 3)}
MEMORIAL = {2021: date(2021, 5, 31), 2022: date(2022, 5, 30),
            2023: date(2023, 5, 29), 2024: date(2024, 5, 27),
            2025: date(2025, 5, 26), 2026: date(2026, 5, 25)}
INDEPENDENCE = {2021: date(2021, 7, 5), 2022: date(2022, 7, 4),
                2023: date(2023, 7, 4), 2024: date(2024, 7, 4),
                2025: date(2025, 7, 4), 2026: date(2026, 7, 3)}
LABOR = {2021: date(2021, 9, 6), 2022: date(2022, 9, 5),
         2023: date(2023, 9, 4), 2024: date(2024, 9, 2),
         2025: date(2025, 9, 1), 2026: date(2026, 9, 7)}
THANKSGIVING = {2021: date(2021, 11, 25), 2022: date(2022, 11, 24),
                2023: date(2023, 11, 23), 2024: date(2024, 11, 28),
                2025: date(2025, 11, 27), 2026: date(2026, 11, 26)}
CHRISTMAS = {2021: date(2021, 12, 24), 2022: date(2022, 12, 26),
             2023: date(2023, 12, 25), 2024: date(2024, 12, 25),
             2025: date(2025, 12, 25), 2026: date(2026, 12, 25)}


@pytest.mark.parametrize("name,table", [
    ("new year", NEW_YEAR), ("good friday", GOOD_FRIDAY),
    ("memorial", MEMORIAL), ("independence", INDEPENDENCE),
    ("labor", LABOR), ("thanksgiving", THANKSGIVING),
    ("christmas", CHRISTMAS),
])
def test_named_holiday_is_present_in_every_covered_year(name, table):
    for year, day in table.items():
        assert is_holiday(day), f"{name} {year} ({day}) missing from the table"


def test_thanksgiving_is_always_the_fourth_thursday():
    for year, day in THANKSGIVING.items():
        assert day.weekday() == 3, f"{day} is not a Thursday"
        assert 22 <= day.day <= 28, f"{day} is not the fourth Thursday"


def test_labor_day_is_always_the_first_monday_of_september():
    for year, day in LABOR.items():
        assert day.weekday() == 0 and day.month == 9 and day.day <= 7


def test_good_friday_is_always_a_friday():
    for year, day in GOOD_FRIDAY.items():
        assert day.weekday() == 4, f"{day} ({year}) is not a Friday"


def test_juneteenth_appears_from_2022_only():
    """It became federal in 2021 but the exchanges first observed it in
    2022, and 2021-06-19 was a Saturday regardless. Absence in 2021 is a
    decision, not an omission."""
    assert not any(d.month == 6 and d.day in (19, 20) for d in HOLIDAYS[2021])
    for year, day in {2022: date(2022, 6, 20), 2023: date(2023, 6, 19),
                      2024: date(2024, 6, 19), 2025: date(2025, 6, 19),
                      2026: date(2026, 6, 19)}.items():
        assert is_holiday(day), f"Juneteenth {year} missing"


def test_2022_deliberately_has_no_new_year_holiday():
    """2022-01-01 was a Saturday and the observance was not shifted."""
    assert not any(d.month == 1 and d.day <= 3 for d in HOLIDAYS[2022])
    assert not is_holiday(date(2022, 1, 3))


# --- early closes -----------------------------------------------------

@pytest.mark.parametrize("day", [
    date(2021, 11, 26), date(2022, 11, 25), date(2023, 11, 24),
    date(2024, 11, 29), date(2025, 11, 28), date(2026, 11, 27),
])
def test_day_after_thanksgiving_is_an_early_close_every_year(day):
    assert early_close_time(day) == time(13, 0)
    assert CAL.rth_close_at(day).astimezone(NY).strftime("%H:%M") == "13:00"


@pytest.mark.parametrize("day", [
    date(2023, 7, 3), date(2024, 7, 3), date(2025, 7, 3),
])
def test_day_before_independence_day_is_an_early_close_when_it_trades(day):
    assert early_close_time(day) == time(13, 0)


def test_christmas_eve_early_close_only_when_it_is_a_weekday_trading_day():
    for day in (date(2024, 12, 24), date(2025, 12, 24), date(2026, 12, 24)):
        assert early_close_time(day) == time(13, 0), day
    # 2021/2022 Christmas Eve fell on a weekend; 2023 on a Sunday.
    for day in (date(2021, 12, 24), date(2022, 12, 24), date(2023, 12, 24)):
        assert early_close_time(day) is None, day


def test_an_ordinary_day_has_a_full_session():
    assert early_close_time(date(2023, 6, 15)) is None
    assert CAL.rth_close_at(date(2023, 6, 15)).astimezone(NY).strftime("%H:%M") == "16:00"


# --- DST across every covered year -----------------------------------

@pytest.mark.parametrize("year", COVERED_YEARS)
def test_rth_open_is_0930_local_in_both_winter_and_summer(year):
    winter = CAL.rth_open_at(date(year, 1, 15))
    summer = CAL.rth_open_at(date(year, 7, 15))
    assert winter.astimezone(NY).strftime("%H:%M") == "09:30"
    assert summer.astimezone(NY).strftime("%H:%M") == "09:30"
    assert winter.hour == 14 and summer.hour == 13     # UTC offset differs


@pytest.mark.parametrize("year,spring,fall", [
    (2021, date(2021, 3, 14), date(2021, 11, 7)),
    (2022, date(2022, 3, 13), date(2022, 11, 6)),
    (2023, date(2023, 3, 12), date(2023, 11, 5)),
    (2024, date(2024, 3, 10), date(2024, 11, 3)),
    (2025, date(2025, 3, 9), date(2025, 11, 2)),
    (2026, date(2026, 3, 8), date(2026, 11, 1)),
])
def test_sessions_either_side_of_each_dst_transition(year, spring, fall):
    """The bug this guards against shifts every opening range by an hour
    for half the year and produces no error at all."""
    for transition in (spring, fall):
        before = transition - timedelta(days=2)
        after = transition + timedelta(days=2)
        for day in (before, after):
            if day.weekday() >= 5 or is_holiday(day):
                continue
            opened = CAL.rth_open_at(day)
            assert opened.astimezone(NY).strftime("%H:%M") == "09:30"
            end = CAL.opening_range_end_at(day, 15)
            assert end.astimezone(NY).strftime("%H:%M") == "09:45"


# --- holiday-aware trading-day arithmetic ----------------------------

def test_trading_day_advances_over_holidays_in_the_new_years():
    # 2021-12-24 (Fri) is Christmas observed; Thursday evening rolls to Monday.
    thursday_evening = datetime(2021, 12, 23, 18, 0, tzinfo=NY)
    assert CAL.session_date_for(thursday_evening) == date(2021, 12, 27)
    # 2022-12-26 (Mon) is Christmas observed; Friday evening rolls to Tuesday.
    friday_evening = datetime(2022, 12, 23, 18, 0, tzinfo=NY)
    assert CAL.session_date_for(friday_evening) == date(2022, 12, 27)


def test_previous_trading_day_skips_holidays():
    assert CAL.previous_trading_day(date(2023, 7, 5)) == date(2023, 7, 3)
    assert CAL.previous_trading_day(date(2021, 7, 6)) == date(2021, 7, 2)


# --- one calendar, used everywhere -----------------------------------

def test_feature_generation_and_research_share_one_session_classification():
    """If the study bucketed bars by a different calendar than the engine
    used to compute features, every session-relative feature would be
    attributed to the wrong day. Same class, same config, same answer."""
    from trading_system.features.engine import FeatureEngine
    from trading_system.market_data import Instrument
    from decimal import Decimal
    instrument = Instrument(symbol="NQ.c.0", product="NQ", tick_size=Decimal("0.25"))
    config = FeatureConfig()
    engine_calendar = FeatureEngine(instrument, config).calendar
    standalone = SessionCalendar(config.session)
    day = DATASET_START
    while day <= DATASET_END:
        for hour in (2, 10, 18, 22):
            t = datetime(day.year, day.month, day.day, hour, tzinfo=NY)
            assert engine_calendar.session_date_for(t) == standalone.session_date_for(t)
        day += timedelta(days=37)


def test_two_engines_with_the_same_config_agree_on_every_boundary():
    from trading_system.features.engine import FeatureEngine
    from trading_system.market_data import Instrument
    from decimal import Decimal
    inst = Instrument(symbol="ES.c.0", product="ES", tick_size=Decimal("0.25"))
    a = FeatureEngine(inst, FeatureConfig()).calendar
    b = FeatureEngine(inst, FeatureConfig()).calendar
    for year in COVERED_YEARS:
        for month in (1, 4, 7, 11):
            d = date(year, month, 15)
            assert a.rth_open_at(d) == b.rth_open_at(d)
            assert a.rth_close_at(d) == b.rth_close_at(d)
