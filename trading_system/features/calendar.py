"""Session and calendar primitives.

Everything downstream keys off "which session is this bar in, and where
in that session". Getting it wrong does not raise -- it silently shifts
the opening range by an hour twice a year, or computes VWAP across a
holiday boundary, and every statistic built on top inherits the error.

THREE THINGS THAT ARE HANDLED EXPLICITLY

  DST. Boundaries are stored as local wall-clock times and resolved
  through the IANA database, so 09:30 New York is 09:30 New York in both
  January and July. Nothing in this module stores or compares UTC
  offsets.

  Holidays and early closes. Data, not code, and versioned with the
  config. The table below is deliberately incomplete-by-design: it
  covers the years we have data for, and `is_known_year` lets callers
  refuse to compute rather than silently treat an unknown year as having
  no holidays. Assuming "not in the list" means "regular session" is how
  a holiday session quietly contaminates a study.

  The session date. A bar at 22:00 New York on Sunday belongs to
  Monday's trading day. Using the calendar date of the timestamp would
  scatter the overnight session across two dates and break every
  prior-day and overnight computation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

from .config import SessionSpec


class SessionPhase(str, Enum):
    RTH = "rth"                 # the cash session
    OVERNIGHT = "overnight"     # previous RTH close -> next RTH open
    CLOSED = "closed"           # weekend / holiday / maintenance halt


# US equity-index futures holidays and early closes, by year. Early close
# maps a date to the local closing time. A year absent from this mapping
# is UNKNOWN, not clean -- see `is_known_year`.
HOLIDAYS = {
    2024: {
        date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19),
        date(2024, 3, 29), date(2024, 5, 27), date(2024, 6, 19),
        date(2024, 7, 4), date(2024, 9, 2), date(2024, 11, 28),
        date(2024, 12, 25),
    },
    2025: {
        date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17),
        date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19),
        date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27),
        date(2025, 12, 25),
    },
    2026: {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
        date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
        date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
        date(2026, 12, 25),
    },
}

EARLY_CLOSES = {
    2024: {date(2024, 7, 3): time(13, 0), date(2024, 11, 29): time(13, 0),
           date(2024, 12, 24): time(13, 0)},
    2025: {date(2025, 7, 3): time(13, 0), date(2025, 11, 28): time(13, 0),
           date(2025, 12, 24): time(13, 0)},
    2026: {date(2026, 11, 27): time(13, 0), date(2026, 12, 24): time(13, 0)},
}


class CalendarError(Exception):
    pass


def is_known_year(year: int) -> bool:
    """Whether the holiday table covers this year at all.

    Callers computing statistics should refuse unknown years rather than
    treat them as holiday-free, which would silently include a half-day
    or a closed session as if it were ordinary.
    """
    return year in HOLIDAYS


def is_holiday(day: date) -> bool:
    if not is_known_year(day.year):
        raise CalendarError(
            f"{day.year} is not in the holiday table; refusing to guess. "
            f"Add the year's holidays rather than treating it as clean."
        )
    return day in HOLIDAYS[day.year]


def early_close_time(day: date) -> Optional[time]:
    return EARLY_CLOSES.get(day.year, {}).get(day)


def _parse_hhmm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


@dataclass(frozen=True)
class SessionContext:
    """Where a single instant sits in the session structure."""
    session_date: date          # the TRADING day this instant belongs to
    phase: SessionPhase
    rth_open_at: datetime       # UTC
    rth_close_at: datetime      # UTC, adjusted for early closes
    local_time: time
    is_early_close: bool

    @property
    def is_rth(self) -> bool:
        return self.phase is SessionPhase.RTH


class SessionCalendar:
    """Resolves instants to sessions for one SessionSpec."""

    def __init__(self, spec: SessionSpec):
        self.spec = spec
        self.tz = ZoneInfo(spec.timezone)
        self._rth_open = _parse_hhmm(spec.rth_open)
        self._rth_close = _parse_hhmm(spec.rth_close)

    # -- trading-day resolution ---------------------------------------

    def session_date_for(self, instant: datetime) -> date:
        """The trading day an instant belongs to.

        Anything at or after the RTH close belongs to the NEXT trading
        day, because that is when the overnight session that precedes
        that day begins. Sunday evening therefore resolves to Monday.
        """
        local = instant.astimezone(self.tz)
        day = local.date()
        if local.time() >= self._rth_close:
            day = self._next_trading_day(day)
        elif day.weekday() >= 5 or self._is_closed_day(day):
            day = self._next_trading_day(day - timedelta(days=1))
        return day

    def _is_closed_day(self, day: date) -> bool:
        return day.weekday() >= 5 or is_holiday(day)

    def _next_trading_day(self, day: date) -> date:
        candidate = day + timedelta(days=1)
        for _ in range(10):
            if not self._is_closed_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise CalendarError(f"no trading day within 10 days of {day}")

    def previous_trading_day(self, day: date) -> date:
        candidate = day - timedelta(days=1)
        for _ in range(10):
            if not self._is_closed_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        raise CalendarError(f"no trading day within 10 days before {day}")

    # -- boundaries ----------------------------------------------------

    def rth_open_at(self, session_date: date) -> datetime:
        """UTC instant of the RTH open. DST-correct by construction: the
        local wall time is fixed and the offset is derived, never stored."""
        local = datetime.combine(session_date, self._rth_open, tzinfo=self.tz)
        return local.astimezone(ZoneInfo("UTC"))

    def rth_close_at(self, session_date: date) -> datetime:
        close = early_close_time(session_date) or self._rth_close
        local = datetime.combine(session_date, close, tzinfo=self.tz)
        return local.astimezone(ZoneInfo("UTC"))

    def opening_range_end_at(self, session_date: date, minutes: int) -> datetime:
        return self.rth_open_at(session_date) + timedelta(minutes=minutes)

    def context_for(self, instant: datetime) -> SessionContext:
        if instant.tzinfo is None:
            raise CalendarError("instant must be timezone-aware")
        session_date = self.session_date_for(instant)
        open_at = self.rth_open_at(session_date)
        close_at = self.rth_close_at(session_date)
        local = instant.astimezone(self.tz)

        if open_at <= instant < close_at:
            phase = SessionPhase.RTH
        elif self._is_closed_day(local.date()) and not (open_at <= instant < close_at):
            # Weekend or holiday: still part of the run-up to the next
            # session, so it is overnight rather than structurally closed,
            # unless it is a full weekend day with no Globex activity.
            phase = SessionPhase.OVERNIGHT if local.weekday() != 5 else SessionPhase.CLOSED
        else:
            phase = SessionPhase.OVERNIGHT

        return SessionContext(
            session_date=session_date,
            phase=phase,
            rth_open_at=open_at,
            rth_close_at=close_at,
            local_time=local.time(),
            is_early_close=early_close_time(session_date) is not None,
        )
