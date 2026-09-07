"""What the engine emits, and the registry of what it can emit.

THE TWO TIMES, AND WHY BOTH EXIST

  effective_at  when the fact became true in market time
  available_at  the earliest instant it could have been KNOWN

For most features these are identical: VWAP at 10:00 is computable at
10:00. For anything requiring subsequent observations they differ, and
the difference is the entire lookahead defence. A swing high at 10:00
confirmed by two later bars is `effective_at=10:00,
available_at=10:02`. Research that filters on effective_at will look
clairvoyant and produce a beautiful, worthless backtest; research must
filter on available_at.

Because that distinction is so easy to get wrong at the query layer, the
`as_of` helper below exists and is the only sanctioned way to ask "what
did we know at time T".

FEATURE vs EVENT

  FEATURE  a state or measurement that has a value at every instant
           (VWAP, ATR, distance to PDH, above/below VWAP)
  EVENT    something that happened at a moment
           (ORB high broken, level swept, FVG formed, BOS)

The ticket's boundary is respected strictly: both are objective and
mechanical. Nothing here scores, ranks or judges quality -- "an A+
setup" is a hypothesis for a later ticket, and no field in this module
could express it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Sequence


class RecordKind(str, Enum):
    FEATURE = "feature"
    EVENT = "event"


class FeatureType(str, Enum):
    """States and measurements. Value-bearing at every observation."""
    SESSION_PHASE = "session_phase"
    VWAP = "vwap"
    SESSION_HIGH = "session_high"
    SESSION_LOW = "session_low"
    PRIOR_DAY_HIGH = "prior_day_high"
    PRIOR_DAY_LOW = "prior_day_low"
    OVERNIGHT_HIGH = "overnight_high"
    OVERNIGHT_LOW = "overnight_low"
    OPENING_RANGE_HIGH = "opening_range_high"
    OPENING_RANGE_LOW = "opening_range_low"
    ATR = "atr"
    REALIZED_VOLATILITY = "realized_volatility"
    BAR_RANGE = "bar_range"
    VOLUME_RATIO = "volume_ratio"
    DISTANCE_TO_VWAP = "distance_to_vwap"
    DISTANCE_TO_OPENING_RANGE_HIGH = "distance_to_opening_range_high"
    DISTANCE_TO_OPENING_RANGE_LOW = "distance_to_opening_range_low"
    DISTANCE_TO_PRIOR_DAY_HIGH = "distance_to_prior_day_high"
    DISTANCE_TO_PRIOR_DAY_LOW = "distance_to_prior_day_low"
    DISTANCE_TO_OVERNIGHT_HIGH = "distance_to_overnight_high"
    DISTANCE_TO_OVERNIGHT_LOW = "distance_to_overnight_low"
    ABOVE_VWAP = "above_vwap"
    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    MINUTES_SINCE_RTH_OPEN = "minutes_since_rth_open"


class EventType(str, Enum):
    """Moments. Objectively defined, mechanically detected."""
    OPENING_RANGE_ESTABLISHED = "opening_range_established"
    OPENING_RANGE_HIGH_BROKEN = "opening_range_high_broken"
    OPENING_RANGE_LOW_BROKEN = "opening_range_low_broken"
    STRUCTURE_BREAK_UP = "structure_break_up"
    STRUCTURE_BREAK_DOWN = "structure_break_down"
    LIQUIDITY_SWEEP_HIGH = "liquidity_sweep_high"
    LIQUIDITY_SWEEP_LOW = "liquidity_sweep_low"
    DISPLACEMENT_UP = "displacement_up"
    DISPLACEMENT_DOWN = "displacement_down"
    FVG_FORMED_UP = "fvg_formed_up"
    FVG_FORMED_DOWN = "fvg_formed_down"
    FVG_FILLED = "fvg_filled"
    FVG_INVERTED = "fvg_inverted"
    SESSION_OPEN = "session_open"
    SESSION_CLOSE = "session_close"


@dataclass(frozen=True)
class FeatureRecord:
    """One deterministic fact about one instrument at one time."""

    instrument_symbol: str
    kind: RecordKind
    type: str                       # FeatureType or EventType value
    effective_at: datetime
    available_at: datetime
    session_date: str               # ISO date of the trading day
    value: Optional[Decimal] = None
    state: Optional[str] = None
    attributes: Dict[str, str] = field(default_factory=dict)

    # provenance
    engine_version: str = "unset"
    config_digest: str = "unset"
    config_name: str = "unset"
    inputs_digest: str = "unset"
    run_id: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("effective_at", "available_at"):
            ts = getattr(self, name)
            if ts.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.available_at < self.effective_at:
            raise ValueError(
                f"available_at {self.available_at.isoformat()} precedes "
                f"effective_at {self.effective_at.isoformat()}: a fact "
                f"cannot be known before it is true"
            )
        if self.value is not None and not isinstance(self.value, Decimal):
            raise ValueError("FeatureRecord.value must be Decimal or None")

    @property
    def confirmation_lag(self):
        """How long after the fact it became knowable. Zero for most."""
        return self.available_at - self.effective_at

    def as_row(self) -> dict:
        return {
            "instrument_symbol": self.instrument_symbol,
            "kind": self.kind.value,
            "type": self.type,
            "effective_at": self.effective_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "session_date": self.session_date,
            "value": None if self.value is None else str(self.value),
            "state": self.state,
            "attributes": dict(sorted(self.attributes.items())),
            "engine_version": self.engine_version,
            "config_digest": self.config_digest,
            "config_name": self.config_name,
            "inputs_digest": self.inputs_digest,
        }


def as_of(records: Sequence[FeatureRecord], instant: datetime) -> Sequence[FeatureRecord]:
    """Everything KNOWABLE at `instant`.

    The only sanctioned way to ask that question. Filtering on
    effective_at instead is the single most likely way to introduce
    lookahead into a study, and it produces results that look excellent
    rather than results that look broken -- which is why this helper
    exists rather than leaving the comparison to each caller.
    """
    return [r for r in records if r.available_at <= instant]
