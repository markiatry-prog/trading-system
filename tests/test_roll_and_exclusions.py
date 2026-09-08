"""Quarterly roll, and the 22 real NQ exclusions it explains.

The revised gate excluded 22 of 1,256 NQ sessions: 19 Fridays, 20 inside
the quarter-end window. The dataset window contains exactly 20 quarterly
expiries, and the per-year counts matched for 2021-2024. This file pins
the mechanism so the coincidence cannot be re-discovered as a mystery.

Fixture-driven tests run against the operator's real
`quality_fixture.json` when it is present and skip otherwise, so CI --
which holds no market data -- stays green while the real behaviour is
still regression-tested where the data lives.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_system.features.roll import (  # noqa: E402
    QUARTERLY_MONTHS, classify_session, expiries_between, is_quarterly_expiry,
    next_session_after, roll_adjacent_sessions, third_friday)

FIXTURE = ROOT / "quality_fixture.json"
WINDOW = (date(2021, 9, 1), date(2026, 9, 1))

# Independently derived, then checked against the rule.
KNOWN_EXPIRIES = [
    date(2021, 9, 17), date(2021, 12, 17), date(2022, 3, 18), date(2022, 6, 17),
    date(2022, 9, 16), date(2022, 12, 16), date(2023, 3, 17), date(2023, 6, 16),
    date(2023, 9, 15), date(2023, 12, 15), date(2024, 3, 15), date(2024, 6, 21),
    date(2024, 9, 20), date(2024, 12, 20), date(2025, 3, 21), date(2025, 6, 20),
    date(2025, 9, 19), date(2025, 12, 19), date(2026, 3, 20), date(2026, 6, 19),
]


# --- the rule ---------------------------------------------------------

def test_third_friday_is_computed_not_tabulated():
    """No table means no uncovered years, unlike the holiday calendar."""
    for day in KNOWN_EXPIRIES:
        assert third_friday(day.year, day.month) == day
        assert day.weekday() == 4
        assert 15 <= day.day <= 21


def test_third_friday_handles_a_month_starting_on_friday():
    """The off-by-one trap: when the 1st IS a Friday, the third is the
    15th, not the 22nd."""
    assert date(2026, 5, 1).weekday() == 4
    assert third_friday(2026, 5) == date(2026, 5, 15)


def test_only_quarter_end_months_carry_an_expiry():
    for month in range(1, 13):
        day = third_friday(2025, month)
        assert is_quarterly_expiry(day) == (month in QUARTERLY_MONTHS)


def test_an_ordinary_friday_is_not_an_expiry():
    for day in (date(2025, 6, 13), date(2025, 6, 27), date(2025, 7, 18)):
        assert not is_quarterly_expiry(day)


def test_the_window_contains_exactly_twenty_expiries():
    """This is the number that matched the 20 in-window exclusions."""
    found = expiries_between(*WINDOW)
    assert found == KNOWN_EXPIRIES
    assert len(found) == 20


def test_expiries_per_year_match_the_reported_exclusion_counts():
    """2021-2024 matched exactly; 2025 and 2026 each had one extra
    exclusion from another cause."""
    from collections import Counter
    per_year = Counter(d.year for d in expiries_between(*WINDOW))
    assert per_year[2021] == 2
    for year in (2022, 2023, 2024, 2025):
        assert per_year[year] == 4
    assert per_year[2026] == 2


# --- roll adjacency ---------------------------------------------------

def _weekdays(start, end):
    days, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def test_the_session_after_an_expiry_is_roll_adjacent():
    """Dropping the expiry alone leaves the NEXT session carrying
    prior-day levels from the previous contract."""
    days = _weekdays(date(2024, 6, 10), date(2024, 6, 28))
    adjacent = roll_adjacent_sessions(days)
    assert date(2024, 6, 24) in adjacent      # Monday after Friday 21st
    assert date(2024, 6, 21) not in adjacent  # the expiry itself


def test_roll_adjacency_skips_weekends_and_uses_real_sessions():
    days = _weekdays(date(2025, 3, 17), date(2025, 3, 28))
    adjacent = roll_adjacent_sessions(days)
    assert adjacent == {date(2025, 3, 24)}    # Mon after Fri 21 Mar


def test_a_missing_following_session_yields_no_adjacency():
    """If the dataset ends on an expiry there is nothing after it."""
    assert roll_adjacent_sessions([date(2026, 6, 19)]) == set()
    assert next_session_after(date(2026, 6, 19), [date(2026, 6, 19)]) is None


def test_classify_session_names_all_three_states():
    days = _weekdays(date(2024, 9, 16), date(2024, 9, 27))
    assert classify_session(date(2024, 9, 20), days) == "expiry"
    assert classify_session(date(2024, 9, 23), days) == "roll_adjacent"
    assert classify_session(date(2024, 9, 26), days) == "ordinary"


def test_every_expiry_in_the_window_has_an_adjacent_session():
    days = _weekdays(*WINDOW)
    adjacent = roll_adjacent_sessions(days)
    # the last expiry (2026-06-19) is followed by sessions inside the window
    assert len(adjacent) == len(KNOWN_EXPIRIES)


# --- fixture-driven: the real observed behaviour ----------------------

pytestmark_fixture = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="quality_fixture.json not present (CI holds no market data)")


def _fixture():
    data = json.loads(FIXTURE.read_text())
    rows = {date.fromisoformat(r["date"]): r for r in data["sessions"]}
    return data, rows


@pytestmark_fixture
def test_fixture_shape_is_what_the_diagnostic_promised():
    data, rows = _fixture()
    assert data["symbol"] == "NQ.c.0"
    assert len(rows) == 1256
    sample = next(iter(rows.values()))
    for key in ("bars", "rth_bars", "rth_coverage", "longest_rth_gap",
                "new_gate_exclusions", "old_gate_reasons"):
        assert key in sample
    assert "price" not in json.dumps(sample).lower()


@pytestmark_fixture
def test_the_revised_gate_excludes_about_twenty_two_sessions():
    _, rows = _fixture()
    excluded = [d for d, r in rows.items() if r["new_gate_exclusions"]]
    assert 15 <= len(excluded) <= 30, f"{len(excluded)} excluded"


@pytestmark_fixture
def test_most_exclusions_are_quarterly_expiries():
    """The claim that must hold for the gate to be signed off: the
    exclusions have a named mechanism, not merely an odd shape."""
    _, rows = _fixture()
    excluded = {d for d, r in rows.items() if r["new_gate_exclusions"]}
    expiries = set(expiries_between(min(rows), max(rows)))
    explained = excluded & expiries
    assert len(explained) >= 0.75 * len(excluded), (
        f"only {len(explained)} of {len(excluded)} exclusions are expiries; "
        f"the rest need a named cause")


@pytestmark_fixture
def test_expiry_sessions_really_do_lack_rth_tape():
    """The mechanism, not just the correlation: the expiring contract
    settles on the opening quotation and stops trading at the cash open,
    so a continuous series pointing at it has little or no RTH tape."""
    _, rows = _fixture()
    expiries = [d for d in expiries_between(min(rows), max(rows)) if d in rows]
    excluded_expiries = [d for d in expiries if rows[d]["new_gate_exclusions"]]
    assert excluded_expiries
    for d in excluded_expiries:
        assert rows[d]["rth_coverage"] < 0.95, (
            f"{d} was excluded but has {rows[d]['rth_coverage']:.2%} RTH "
            f"coverage; the expiry mechanism does not explain it")


@pytestmark_fixture
def test_ordinary_sessions_overwhelmingly_pass():
    """The old gate excluded 695. Anything near that again is a bug."""
    _, rows = _fixture()
    expiries = set(expiries_between(min(rows), max(rows)))
    ordinary = {d: r for d, r in rows.items() if d not in expiries}
    failed = [d for d, r in ordinary.items() if r["new_gate_exclusions"]]
    assert len(failed) / len(ordinary) < 0.02, (
        f"{len(failed)}/{len(ordinary)} ordinary sessions excluded")


@pytestmark_fixture
def test_the_old_gate_would_still_reject_most_sessions():
    """Kept so the improvement is measured, not asserted."""
    _, rows = _fixture()
    old_failed = [d for d, r in rows.items() if r["old_gate_reasons"]]
    assert len(old_failed) > 400, (
        "the fixture should still record the old gate's behaviour")
    ratio_rule = [d for d, r in rows.items()
                  if "max_single_bar_move_atr" in r["old_gate_reasons"]]
    assert len(ratio_rule) > 0.75 * len(old_failed), (
        "the range-ratio rule should account for most old exclusions")
