"""The deterministic feature engine.

STRUCTURALLY INCAPABLE OF LOOKAHEAD

The engine consumes bars one at a time through `observe()` and holds
only state derived from bars it has already seen. There is no sequence
to index into and no "future" variable to read, so the common source of
leakage -- computing over a whole array and slicing afterwards -- is not
expressible here. That is a design choice, not a convention: an engine
handed the full series would need discipline to avoid peeking, and
discipline is not a control.

The one genuinely hard case is a pivot, which cannot be known until
later bars confirm it. That is handled by emitting the record when
confirmation arrives, stamped `effective_at` = the pivot's own bar and
`available_at` = the confirming bar. The fact is dated honestly and its
knowability is dated honestly, and research filtering on available_at
gets the truth.

NO AI ANYWHERE. Every value here comes from arithmetic on observed bars.
There is no model call, no scoring, no judgement, and no field capable of
carrying one.

NOT STRATEGY. An ORB break is recorded because it is objectively
definable, not because breaks are believed to be profitable. Nothing in
this module ranks, filters or prefers.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from ..market_data import Bar, Instrument
from ..provenance import digest
from .calendar import SessionCalendar, SessionPhase
from .config import DEFAULT_CONFIG, FeatureConfig
from .records import EventType, FeatureRecord, FeatureType, RecordKind

# Bump when the COMPUTATION changes. A config change is recorded
# separately; this distinguishes "we changed the rule" from "we changed
# the parameter", which is the question the research system must be able
# to answer.
ENGINE_VERSION = "1.0.0"

ZERO = Decimal("0")


@dataclass
class _FVG:
    """A fair value gap: an unfilled three-bar imbalance."""
    direction: str        # "up" | "down"
    top: Decimal
    bottom: Decimal
    formed_at: datetime   # middle bar (effective)
    confirmed_at: datetime
    filled: bool = False
    inverted: bool = False


class FeatureEngine:
    """Bar in, records out. One instrument, one config."""

    def __init__(self, instrument: Instrument, config: FeatureConfig = DEFAULT_CONFIG,
                 run_id: Optional[str] = None):
        self.instrument = instrument
        self.config = config
        self.run_id = run_id
        self.calendar = SessionCalendar(config.session)
        self._config_digest = config.digest()

        # rolling windows
        self._bars: Deque[Bar] = deque(maxlen=max(
            config.swing_lookback_bars * 2 + 1,
            config.atr_period_bars + 1,
            config.realized_vol_window_bars + 1,
            config.volume_average_window_bars + 1,
            3,
        ))
        self._true_ranges: Deque[Decimal] = deque(maxlen=config.atr_period_bars)
        self._returns: Deque[Decimal] = deque(maxlen=config.realized_vol_window_bars)
        self._volumes: Deque[int] = deque(maxlen=config.volume_average_window_bars)

        # session state
        self._session_date: Optional[date] = None
        self._session_high: Optional[Decimal] = None
        self._session_low: Optional[Decimal] = None
        self._rth_high: Optional[Decimal] = None
        self._rth_low: Optional[Decimal] = None
        self._overnight_high: Optional[Decimal] = None
        self._overnight_low: Optional[Decimal] = None
        self._vwap_pv = ZERO
        self._vwap_volume = 0

        # carried across sessions
        self._prior_day_high: Optional[Decimal] = None
        self._prior_day_low: Optional[Decimal] = None
        self._prior_overnight_high: Optional[Decimal] = None
        self._prior_overnight_low: Optional[Decimal] = None

        # opening range
        self._or_high: Optional[Decimal] = None
        self._or_low: Optional[Decimal] = None
        self._or_established = False
        self._or_high_broken = False
        self._or_low_broken = False

        # structure
        self._confirmed_swing_highs: List[Tuple[datetime, Decimal]] = []
        self._confirmed_swing_lows: List[Tuple[datetime, Decimal]] = []
        self._last_structure_high: Optional[Decimal] = None
        self._last_structure_low: Optional[Decimal] = None

        # gaps
        self._open_fvgs: List[_FVG] = []

        self._seen_observed_at: Optional[datetime] = None
        self._inputs: List[str] = []

    # -- public API ----------------------------------------------------

    def observe(self, bar: Bar) -> List[FeatureRecord]:
        """Process one bar; return everything knowable as of its close.

        Duplicate and out-of-order bars are refused rather than absorbed.
        A silently-absorbed duplicate double-counts VWAP volume; a
        silently-reordered bar corrupts every rolling window. Both would
        be invisible in the output.
        """
        if bar.instrument.symbol != self.instrument.symbol:
            raise ValueError(
                f"engine is for {self.instrument.symbol}, got {bar.instrument.symbol}"
            )
        if self._seen_observed_at is not None:
            if bar.observed_at == self._seen_observed_at:
                raise ValueError(
                    f"duplicate bar at {bar.observed_at.isoformat()}: would "
                    f"double-count VWAP volume and corrupt rolling windows"
                )
            if bar.observed_at < self._seen_observed_at:
                raise ValueError(
                    f"out-of-order bar {bar.observed_at.isoformat()} after "
                    f"{self._seen_observed_at.isoformat()}"
                )
        self._seen_observed_at = bar.observed_at
        self._inputs.append(f"{bar.observed_at.isoformat()}|{bar.close}|{bar.volume}")

        out: List[FeatureRecord] = []
        ctx = self.calendar.context_for(bar.observed_at)
        at = bar.closed_at   # nothing about a bar is knowable before it closes

        if self._session_date != ctx.session_date:
            self._roll_session(ctx.session_date, out, at)

        self._update_session_extremes(bar, ctx)
        self._update_rolling(bar)
        self._update_opening_range(bar, ctx, out, at)

        out.extend(self._emit_state(bar, ctx, at))
        out.extend(self._detect_sweeps(bar, at))
        out.extend(self._detect_displacement(bar, at))
        # Structure breaks are checked BEFORE this bar's own pivot can be
        # confirmed, so a break is always measured against a level that
        # was already established. The alternative -- confirming a pivot
        # and breaking it on the same bar -- is defensible but muddier,
        # and this ordering is the conservative one.
        out.extend(self._detect_structure_breaks(bar, at))
        # FVGs need (first, middle, current); `_bars` still excludes the
        # current bar here, which is exactly the triple required.
        out.extend(self._detect_fvgs(bar, at))
        out.extend(self._update_fvg_states(bar, at))
        out.extend(self._detect_or_breaks(bar, ctx, at))

        self._bars.append(bar)
        # Pivot confirmation needs k bars either side, so it runs only
        # once the current bar is in the window.
        out.extend(self._detect_swings(at))
        return out

    def run(self, bars: Iterable[Bar]) -> List[FeatureRecord]:
        records: List[FeatureRecord] = []
        for bar in bars:
            records.extend(self.observe(bar))
        return records

    def inputs_digest(self) -> str:
        return digest(self._inputs)

    # -- record construction -------------------------------------------

    def _record(self, kind, type_, effective_at, available_at, session_date,
                value=None, state=None, **attrs) -> FeatureRecord:
        return FeatureRecord(
            instrument_symbol=self.instrument.symbol,
            kind=kind,
            type=type_.value if hasattr(type_, "value") else str(type_),
            effective_at=effective_at,
            available_at=available_at,
            session_date=session_date.isoformat(),
            value=value,
            state=state,
            attributes={k: str(v) for k, v in attrs.items()},
            engine_version=ENGINE_VERSION,
            config_digest=self._config_digest,
            config_name=self.config.name,
            inputs_digest="streaming",
            run_id=self.run_id,
        )

    def _feature(self, type_, at, session_date, value=None, state=None, **attrs):
        return self._record(RecordKind.FEATURE, type_, at, at, session_date,
                            value, state, **attrs)

    def _event(self, type_, effective_at, available_at, session_date, value=None,
               state=None, **attrs):
        return self._record(RecordKind.EVENT, type_, effective_at, available_at,
                            session_date, value, state, **attrs)

    # -- session -------------------------------------------------------

    def _roll_session(self, new_date: date, out: List[FeatureRecord], at: datetime):
        if self._session_date is not None:
            # The completed day's RTH extremes become the prior day's.
            self._prior_day_high = self._rth_high
            self._prior_day_low = self._rth_low
            self._prior_overnight_high = self._overnight_high
            self._prior_overnight_low = self._overnight_low
            out.append(self._event(EventType.SESSION_CLOSE, at, at, self._session_date))
        self._session_date = new_date
        self._session_high = self._session_low = None
        self._rth_high = self._rth_low = None
        self._overnight_high = self._overnight_low = None
        self._vwap_pv = ZERO
        self._vwap_volume = 0
        self._or_high = self._or_low = None
        self._or_established = False
        self._or_high_broken = self._or_low_broken = False
        out.append(self._event(EventType.SESSION_OPEN, at, at, new_date))

    def _update_session_extremes(self, bar: Bar, ctx):
        self._session_high = bar.high if self._session_high is None else max(self._session_high, bar.high)
        self._session_low = bar.low if self._session_low is None else min(self._session_low, bar.low)
        if ctx.phase is SessionPhase.RTH:
            self._rth_high = bar.high if self._rth_high is None else max(self._rth_high, bar.high)
            self._rth_low = bar.low if self._rth_low is None else min(self._rth_low, bar.low)
            # VWAP is an RTH construct here: including the thin overnight
            # tape makes the level unrecognisable against every charting
            # package a human would compare it to.
            typical = (bar.high + bar.low + bar.close) / 3
            self._vwap_pv += typical * bar.volume
            self._vwap_volume += bar.volume
        else:
            self._overnight_high = bar.high if self._overnight_high is None else max(self._overnight_high, bar.high)
            self._overnight_low = bar.low if self._overnight_low is None else min(self._overnight_low, bar.low)

    def _update_rolling(self, bar: Bar):
        if self._bars:
            prev = self._bars[-1]
            tr = max(bar.high - bar.low,
                     abs(bar.high - prev.close),
                     abs(bar.low - prev.close))
            if prev.close != 0:
                self._returns.append((bar.close - prev.close) / prev.close)
        else:
            tr = bar.high - bar.low
        self._true_ranges.append(tr)
        self._volumes.append(bar.volume)

    def _atr(self) -> Optional[Decimal]:
        if len(self._true_ranges) < self.config.atr_period_bars:
            return None
        return sum(self._true_ranges) / len(self._true_ranges)

    def _vwap(self) -> Optional[Decimal]:
        if self._vwap_volume == 0:
            return None
        return self._vwap_pv / self._vwap_volume

    # -- opening range -------------------------------------------------

    def _update_opening_range(self, bar: Bar, ctx, out, at):
        if ctx.phase is not SessionPhase.RTH or self._or_established:
            return
        or_end = self.calendar.opening_range_end_at(
            ctx.session_date, self.config.opening_range_minutes)
        if bar.observed_at < or_end:
            self._or_high = bar.high if self._or_high is None else max(self._or_high, bar.high)
            self._or_low = bar.low if self._or_low is None else min(self._or_low, bar.low)
            if bar.closed_at >= or_end:
                self._establish_or(out, bar.closed_at, ctx)
        elif self._or_high is not None:
            self._establish_or(out, at, ctx)

    def _establish_or(self, out, at, ctx):
        self._or_established = True
        out.append(self._event(
            EventType.OPENING_RANGE_ESTABLISHED, at, at, ctx.session_date,
            value=self._or_high - self._or_low,
            high=self._or_high, low=self._or_low,
            minutes=self.config.opening_range_minutes))

    def _detect_or_breaks(self, bar: Bar, ctx, at) -> List[FeatureRecord]:
        out = []
        if not self._or_established or ctx.phase is not SessionPhase.RTH:
            return out
        if not self._or_high_broken and bar.close > self._or_high:
            self._or_high_broken = True
            out.append(self._event(EventType.OPENING_RANGE_HIGH_BROKEN, at, at,
                                   ctx.session_date, value=bar.close,
                                   level=self._or_high))
        if not self._or_low_broken and bar.close < self._or_low:
            self._or_low_broken = True
            out.append(self._event(EventType.OPENING_RANGE_LOW_BROKEN, at, at,
                                   ctx.session_date, value=bar.close,
                                   level=self._or_low))
        return out

    # -- state features ------------------------------------------------

    def _emit_state(self, bar: Bar, ctx, at) -> List[FeatureRecord]:
        sd = ctx.session_date
        out = [
            self._feature(FeatureType.SESSION_PHASE, at, sd, state=ctx.phase.value),
            self._feature(FeatureType.BAR_RANGE, at, sd, value=bar.high - bar.low),
        ]
        minutes_in = (bar.observed_at - ctx.rth_open_at).total_seconds() / 60
        out.append(self._feature(FeatureType.MINUTES_SINCE_RTH_OPEN, at, sd,
                                 value=Decimal(int(minutes_in))))

        vwap = self._vwap()
        if vwap is not None:
            out.append(self._feature(FeatureType.VWAP, at, sd, value=vwap))
            out.append(self._feature(FeatureType.DISTANCE_TO_VWAP, at, sd,
                                     value=bar.close - vwap))
            out.append(self._feature(FeatureType.ABOVE_VWAP, at, sd,
                                     state="above" if bar.close > vwap else "below"))
        atr = self._atr()
        if atr is not None:
            out.append(self._feature(FeatureType.ATR, at, sd, value=atr))
        if len(self._returns) == self.config.realized_vol_window_bars:
            mean = sum(self._returns) / len(self._returns)
            var = sum((r - mean) ** 2 for r in self._returns) / len(self._returns)
            out.append(self._feature(FeatureType.REALIZED_VOLATILITY, at, sd,
                                     value=var.sqrt()))
        if self._volumes:
            # Decimal throughout: sum(ints)/len is a FLOAT in Python 3,
            # and mixing it with Decimal raises. Worse, had it not raised
            # it would have silently seeded float error into a stored
            # feature value.
            avg = Decimal(sum(self._volumes)) / Decimal(len(self._volumes))
            if avg > 0:
                out.append(self._feature(FeatureType.VOLUME_RATIO, at, sd,
                                         value=Decimal(bar.volume) / avg))

        for ftype, level in (
            (FeatureType.SESSION_HIGH, self._session_high),
            (FeatureType.SESSION_LOW, self._session_low),
            (FeatureType.PRIOR_DAY_HIGH, self._prior_day_high),
            (FeatureType.PRIOR_DAY_LOW, self._prior_day_low),
            (FeatureType.OVERNIGHT_HIGH, self._prior_overnight_high),
            (FeatureType.OVERNIGHT_LOW, self._prior_overnight_low),
            (FeatureType.OPENING_RANGE_HIGH, self._or_high if self._or_established else None),
            (FeatureType.OPENING_RANGE_LOW, self._or_low if self._or_established else None),
        ):
            if level is not None:
                out.append(self._feature(ftype, at, sd, value=level))

        for ftype, level in (
            (FeatureType.DISTANCE_TO_PRIOR_DAY_HIGH, self._prior_day_high),
            (FeatureType.DISTANCE_TO_PRIOR_DAY_LOW, self._prior_day_low),
            (FeatureType.DISTANCE_TO_OVERNIGHT_HIGH, self._prior_overnight_high),
            (FeatureType.DISTANCE_TO_OVERNIGHT_LOW, self._prior_overnight_low),
            (FeatureType.DISTANCE_TO_OPENING_RANGE_HIGH,
             self._or_high if self._or_established else None),
            (FeatureType.DISTANCE_TO_OPENING_RANGE_LOW,
             self._or_low if self._or_established else None),
        ):
            if level is not None:
                out.append(self._feature(ftype, at, sd, value=bar.close - level))
        return out

    # -- structure -----------------------------------------------------

    def _detect_swings(self, at) -> List[FeatureRecord]:
        """A pivot needs k bars either side, so it can only be confirmed k
        bars late. Both times are recorded; nothing is back-dated."""
        k = self.config.swing_lookback_bars
        out: List[FeatureRecord] = []
        if len(self._bars) < 2 * k + 1:
            return out
        window = list(self._bars)[-(2 * k + 1):]
        candidate = window[k]          # exactly k bars either side
        left = window[:k]
        right = window[k + 1:]
        sd = self.calendar.session_date_for(candidate.observed_at)
        if all(candidate.high > b.high for b in left + right):
            self._confirmed_swing_highs.append((candidate.observed_at, candidate.high))
            self._last_structure_high = candidate.high
            out.append(self._record(RecordKind.FEATURE, FeatureType.SWING_HIGH,
                                    candidate.observed_at, at, sd,
                                    value=candidate.high))
        if all(candidate.low < b.low for b in left + right):
            self._confirmed_swing_lows.append((candidate.observed_at, candidate.low))
            self._last_structure_low = candidate.low
            out.append(self._record(RecordKind.FEATURE, FeatureType.SWING_LOW,
                                    candidate.observed_at, at, sd,
                                    value=candidate.low))
        return out

    def _detect_structure_breaks(self, bar: Bar, at) -> List[FeatureRecord]:
        out = []
        sd = self.calendar.session_date_for(bar.observed_at)
        if self._last_structure_high is not None and bar.close > self._last_structure_high:
            out.append(self._event(EventType.STRUCTURE_BREAK_UP, at, at, sd,
                                   value=bar.close, level=self._last_structure_high))
            self._last_structure_high = None
        if self._last_structure_low is not None and bar.close < self._last_structure_low:
            out.append(self._event(EventType.STRUCTURE_BREAK_DOWN, at, at, sd,
                                   value=bar.close, level=self._last_structure_low))
            self._last_structure_low = None
        return out

    # -- liquidity -----------------------------------------------------

    def _detect_sweeps(self, bar: Bar, at) -> List[FeatureRecord]:
        """Wick beyond a reference level, close back inside. The
        close-back is what separates a sweep from a break."""
        out = []
        sd = self.calendar.session_date_for(bar.observed_at)
        tick = self.instrument.tick_size
        margin = tick * self.config.sweep_min_ticks
        for name, level in (("prior_day_high", self._prior_day_high),
                            ("overnight_high", self._prior_overnight_high)):
            if level is not None and bar.high >= level + margin and bar.close < level:
                out.append(self._event(EventType.LIQUIDITY_SWEEP_HIGH, at, at, sd,
                                       value=bar.high, level=level, reference=name))
        for name, level in (("prior_day_low", self._prior_day_low),
                            ("overnight_low", self._prior_overnight_low)):
            if level is not None and bar.low <= level - margin and bar.close > level:
                out.append(self._event(EventType.LIQUIDITY_SWEEP_LOW, at, at, sd,
                                       value=bar.low, level=level, reference=name))
        return out

    def _detect_displacement(self, bar: Bar, at) -> List[FeatureRecord]:
        atr = self._atr()
        if atr is None or atr == 0:
            return []
        tr = self._true_ranges[-1]
        if tr < atr * self.config.displacement_atr_multiple:
            return []
        sd = self.calendar.session_date_for(bar.observed_at)
        etype = EventType.DISPLACEMENT_UP if bar.close > bar.open else EventType.DISPLACEMENT_DOWN
        return [self._event(etype, at, at, sd, value=tr,
                            atr=atr, multiple=(tr / atr))]

    # -- fair value gaps -----------------------------------------------

    def _detect_fvgs(self, bar: Bar, at) -> List[FeatureRecord]:
        """Three-bar imbalance, where the current bar is the third.

        The gap exists only once this bar closes, so `available_at` is
        now, while `effective_at` is the middle bar that defines it. This
        is the clearest small example of why the two times are separate.
        """
        if len(self._bars) < 2:
            return []
        first, middle = self._bars[-2], self._bars[-1]
        out = []
        sd = self.calendar.session_date_for(middle.observed_at)
        min_gap = self.instrument.tick_size * self.config.fvg_min_ticks
        if bar.low > first.high and (bar.low - first.high) >= min_gap:
            gap = _FVG("up", bar.low, first.high, middle.observed_at, at)
            self._open_fvgs.append(gap)
            out.append(self._record(RecordKind.EVENT, EventType.FVG_FORMED_UP,
                                    middle.observed_at, at, sd,
                                    value=bar.low - first.high,
                                    top=bar.low, bottom=first.high))
        if first.low > bar.high and (first.low - bar.high) >= min_gap:
            gap = _FVG("down", first.low, bar.high, middle.observed_at, at)
            self._open_fvgs.append(gap)
            out.append(self._record(RecordKind.EVENT, EventType.FVG_FORMED_DOWN,
                                    middle.observed_at, at, sd,
                                    value=first.low - bar.high,
                                    top=first.low, bottom=bar.high))
        return out

    def _update_fvg_states(self, bar: Bar, at) -> List[FeatureRecord]:
        """Fill and inversion are separate, objectively defined states.

        Filled: price traded back into the gap.
        Inverted: price closed entirely through it, so a gap that was
        support has become resistance (or the reverse). Recorded as a
        transition because that is deterministically definable; whether
        it means anything is a later question.
        """
        out = []
        sd = self.calendar.session_date_for(bar.observed_at)
        for gap in self._open_fvgs:
            if gap.inverted:
                continue
            if not gap.filled and bar.low <= gap.top and bar.high >= gap.bottom:
                gap.filled = True
                out.append(self._event(EventType.FVG_FILLED, at, at, sd,
                                       value=bar.close, direction=gap.direction,
                                       top=gap.top, bottom=gap.bottom))
            if gap.filled and not gap.inverted:
                through = (bar.close < gap.bottom) if gap.direction == "up" else (bar.close > gap.top)
                if through:
                    gap.inverted = True
                    out.append(self._event(EventType.FVG_INVERTED, at, at, sd,
                                           value=bar.close, direction=gap.direction,
                                           top=gap.top, bottom=gap.bottom))
        return out
