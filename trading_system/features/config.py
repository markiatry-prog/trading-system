"""Versioned feature configuration.

Every trading parameter lives here, never in the code that uses it. The
reason is not tidiness: a threshold buried in a function makes "did ORB
v3 and ORB v4 differ because of the rule or because of the parameter?"
unanswerable after the fact, and that question is the whole point of the
research system this feeds.

Each config carries a digest. Two runs with the same engine version and
the same digest must produce byte-identical output; a differing digest is
the honest explanation for differing results.

ON DEFAULTS. These are defensible starting points, not claims about what
works. Nothing here asserts that a 15-minute opening range is better than
a 30-minute one -- that is exactly what T-004 exists to test. The
defaults are chosen to be conventional so that results are comparable
with published work, and every one of them is overridable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Tuple

from ..provenance import digest


@dataclass(frozen=True)
class SessionSpec:
    """Session boundaries in exchange-local wall clock.

    Stored as local time plus a timezone name rather than as UTC offsets,
    because the RTH open is 09:30 New York in both January and July while
    the UTC offset is not. Anything storing offsets is wrong twice a year.
    """
    timezone: str = "America/New_York"
    rth_open: str = "09:30"
    rth_close: str = "16:00"
    # Globex runs Sunday 18:00 ET to Friday 17:00 ET with a daily
    # maintenance halt. Overnight, for our purposes, is simply everything
    # between the previous RTH close and the next RTH open.
    eth_open: str = "18:00"
    eth_close: str = "17:00"


@dataclass(frozen=True)
class FeatureConfig:
    """The complete parameter set for one feature-engine run."""

    # --- identity ---
    name: str = "baseline"

    # --- sessions ---
    session: SessionSpec = field(default_factory=SessionSpec)

    # --- opening range ---
    # 15 minutes is the most common convention in published ORB work.
    # 5 and 30 are the obvious alternatives and are why this is a field.
    opening_range_minutes: int = 15

    # --- swing / pivot structure ---
    # A bar is a swing high if it is the highest of the k bars either
    # side. k=2 is the standard "fractal". Larger k means fewer, more
    # significant pivots and a LONGER confirmation delay -- which is a
    # lookahead cost, not just a sensitivity knob.
    swing_lookback_bars: int = 2

    # --- displacement ---
    # A bar whose true range exceeds this multiple of ATR. 1.5 is a
    # deliberately loose starting point: too tight and the primitive
    # fires so rarely that no statistics are possible.
    displacement_atr_multiple: Decimal = Decimal("1.5")
    atr_period_bars: int = 14

    # --- fair value gaps ---
    # Minimum gap size in ticks. 1 tick would admit noise on every other
    # bar; 4 ticks (1 NQ point) is a defensible floor that still leaves
    # plenty of samples.
    fvg_min_ticks: int = 4

    # --- liquidity sweeps ---
    # A sweep requires the wick to exceed the level by at least this many
    # ticks AND the bar to close back inside. Requiring a close-back is
    # what distinguishes a sweep from a genuine break.
    sweep_min_ticks: int = 1

    # --- volatility / context ---
    realized_vol_window_bars: int = 30
    volume_average_window_bars: int = 30

    def digest(self) -> str:
        """Stable hash of every parameter."""
        return digest(self.as_dict())

    def as_dict(self) -> dict:
        raw = asdict(self)
        # Decimals must serialise as strings, not floats: json would turn
        # Decimal("1.5") into 1.5 and two configs that differ in the last
        # decimal place would hash identically.
        raw["displacement_atr_multiple"] = str(self.displacement_atr_multiple)
        return raw

    def version_label(self) -> str:
        return f"{self.name}@{self.digest()[:12]}"


DEFAULT_CONFIG = FeatureConfig()
