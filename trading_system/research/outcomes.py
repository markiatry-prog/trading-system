"""Forward-path measurement: MFE, MAE, and the ORDER they occurred in.

THE DISTINCTION THIS MODULE EXISTS TO MAKE

A PREDICTIVE edge is a statistical association between a condition and a
later price. A TRADABLE edge is one you could actually have captured.
They are not the same, and the gap between them is mostly about
ORDERING.

Consider an event after which price reliably ends +20 points. That looks
excellent. If the path first went -30 points and only then to +20, any
position sized to survive -30 is a different trade from the one the
summary statistic implies, and one stopped at -15 never saw the +20 at
all. Terminal return alone cannot distinguish these. Excursion ordering
can, so it is recorded for every event.

WHAT IS DELIBERATELY NOT HERE

No entry model, no stop placement, no position sizing, no grading. This
module measures what the market did after an event. Whether that is
worth trading is a later question, and this module is what will make it
answerable rather than guessable.

DIRECTION IS SUPPLIED, NEVER INFERRED

`favorable` depends on which way a hypothesis says price should go, and
that is declared at pre-registration. Choosing the direction after
seeing the path would double the effective number of tests while
appearing to be one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple

from ..market_data import Bar
from .hypotheses import Direction


class OutcomeError(Exception):
    pass


@dataclass(frozen=True)
class ForwardPath:
    """What happened after one event, over one horizon.

    All excursions are signed in POINTS relative to the reference price,
    which is the close of the first bar the event could have been acted
    on. Nothing here is normalised by ATR or converted to R-multiples:
    both require choices that belong to a later ticket, and baking them
    in now would hide the raw path.
    """
    event_type: str
    event_at: datetime            # the event's available_at
    reference_price: Decimal      # close of the first actionable bar
    reference_at: datetime
    horizon_minutes: int
    bars_observed: int

    terminal_return: Decimal      # last close - reference
    max_up: Decimal               # highest high - reference  (>= 0)
    max_down: Decimal             # lowest low - reference    (<= 0)
    max_up_at: Optional[datetime]
    max_down_at: Optional[datetime]
    truncated: bool               # horizon ran past available data

    # -- direction-aware views ----------------------------------------

    def mfe(self, direction: Direction) -> Decimal:
        """Maximum favorable excursion, given the declared direction."""
        return self.max_up if direction is Direction.UP else -self.max_down

    def mae(self, direction: Direction) -> Decimal:
        """Maximum adverse excursion, as a POSITIVE magnitude."""
        return -self.max_down if direction is Direction.UP else self.max_up

    def signed_return(self, direction: Direction) -> Decimal:
        return self.terminal_return if direction is Direction.UP else -self.terminal_return

    def favorable_came_first(self, direction: Direction) -> Optional[bool]:
        """Did the best point arrive before the worst?

        None when either never moved, which is not the same as False and
        must not be silently treated as such.
        """
        fav_at = self.max_up_at if direction is Direction.UP else self.max_down_at
        adv_at = self.max_down_at if direction is Direction.UP else self.max_up_at
        if fav_at is None or adv_at is None:
            return None
        if fav_at == adv_at:
            return None      # same bar; the path within it is unknown
        return fav_at < adv_at



@dataclass
class PathScanner:
    """Holds the bars so threshold questions can be answered exactly.

    Kept separate from ForwardPath because a summary should be cheap to
    store and pass around, while threshold scanning needs the raw path.
    """
    reference_price: Decimal
    bars: Sequence[Bar]

    def hit_first(self, direction: Direction, favorable_points: Decimal,
                  adverse_points: Decimal) -> Tuple[str, Optional[datetime]]:
        if favorable_points <= 0 or adverse_points <= 0:
            raise OutcomeError("thresholds must be positive magnitudes")
        if direction is Direction.UP:
            fav_level = self.reference_price + favorable_points
            adv_level = self.reference_price - adverse_points
        else:
            fav_level = self.reference_price - favorable_points
            adv_level = self.reference_price + adverse_points
        for bar in self.bars:
            if direction is Direction.UP:
                touched_fav = bar.high >= fav_level
                touched_adv = bar.low <= adv_level
            else:
                touched_fav = bar.low <= fav_level
                touched_adv = bar.high >= adv_level
            if touched_fav and touched_adv:
                # Both inside one bar. Which came first is genuinely
                # unknowable from bar data. Reporting it as a win would
                # be the single most flattering lie available here.
                return "same_bar", bar.observed_at
            if touched_fav:
                return "favorable", bar.observed_at
            if touched_adv:
                return "adverse", bar.observed_at
        return "neither", None


def measure_forward_path(event_type: str, event_at: datetime,
                         bars: Sequence[Bar], horizon_minutes: int
                         ) -> Optional[Tuple[ForwardPath, PathScanner]]:
    """Measure what followed an event.

    NO LOOKAHEAD: the reference is the close of the first bar that CLOSES
    at or after the event became available, and only bars from that point
    forward are considered. Using the event bar's own open, or a bar that
    was still forming, would import information the event did not have.

    Returns None when no actionable bar exists after the event.
    """
    if horizon_minutes <= 0:
        raise OutcomeError("horizon must be positive")

    actionable = [b for b in bars if b.closed_at >= event_at]
    if not actionable:
        return None
    reference_bar = actionable[0]
    reference_price = reference_bar.close
    window_end = reference_bar.closed_at + timedelta(minutes=horizon_minutes)
    # Strictly AFTER the reference bar: the reference close is the price
    # you got, not part of the path you then experienced.
    path = [b for b in actionable[1:] if b.closed_at <= window_end]

    if not path:
        return ForwardPath(
            event_type=event_type, event_at=event_at,
            reference_price=reference_price, reference_at=reference_bar.closed_at,
            horizon_minutes=horizon_minutes, bars_observed=0,
            terminal_return=Decimal(0), max_up=Decimal(0), max_down=Decimal(0),
            max_up_at=None, max_down_at=None, truncated=True,
        ), PathScanner(reference_price, [])

    max_up = Decimal(0)
    max_down = Decimal(0)
    max_up_at: Optional[datetime] = None
    max_down_at: Optional[datetime] = None
    for bar in path:
        up = bar.high - reference_price
        down = bar.low - reference_price
        # Strict inequality on purpose: the FIRST time an extreme is
        # reached is what matters for ordering. Using >= would re-stamp
        # the time on every later bar that merely equalled it, and the
        # favourable-came-first question would then be answered wrongly.
        if up > max_up:
            max_up, max_up_at = up, bar.observed_at
        if down < max_down:
            max_down, max_down_at = down, bar.observed_at

    # An event whose path never exceeded the reference in a direction has
    # no excursion time in it; None, not the first bar.
    if max_up == 0:
        max_up_at = None
    if max_down == 0:
        max_down_at = None

    last_covered = path[-1].closed_at
    truncated = last_covered < window_end

    return ForwardPath(
        event_type=event_type, event_at=event_at,
        reference_price=reference_price, reference_at=reference_bar.closed_at,
        horizon_minutes=horizon_minutes, bars_observed=len(path),
        terminal_return=path[-1].close - reference_price,
        max_up=max_up, max_down=max_down,
        max_up_at=max_up_at, max_down_at=max_down_at,
        truncated=truncated,
    ), PathScanner(reference_price, path)
