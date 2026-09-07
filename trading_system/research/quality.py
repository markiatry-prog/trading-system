"""Data-quality gating. Runs BEFORE any statistic is computed.

Real market data contains gaps, halts, roll discontinuities, bad prints
and session anomalies. None of these announce themselves. A single
contaminated day can manufacture an apparent edge, and once it is in the
sample nothing downstream can detect it.

The gate therefore REFUSES days rather than flagging them, and every
exclusion is recorded with its reason so the denominator in any result
is honest: "412 of 500 days, 88 excluded" is a finding; "412 days" alone
hides one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from ..market_data import Bar


@dataclass(frozen=True)
class QualityThresholds:
    min_bars: int = 300               # an RTH session is ~390 one-minute bars
    max_missing_fraction: Decimal = Decimal("0.10")
    max_single_bar_move_atr: Decimal = Decimal("10")   # bad-print detector
    min_total_volume: int = 1000
    max_zero_volume_fraction: Decimal = Decimal("0.30")


@dataclass
class DayQuality:
    session_date: date
    instrument_symbol: str
    passed: bool
    reasons: List[str] = field(default_factory=list)
    bar_count: int = 0
    total_volume: int = 0

    def as_row(self) -> dict:
        return {"session_date": self.session_date.isoformat(),
                "instrument_symbol": self.instrument_symbol,
                "passed": self.passed, "reasons": sorted(self.reasons),
                "bar_count": self.bar_count, "total_volume": self.total_volume}


def assess_day(session_date: date, instrument_symbol: str, bars: Sequence[Bar],
               expected_bars: int = 390,
               thresholds: QualityThresholds = QualityThresholds()) -> DayQuality:
    reasons: List[str] = []
    q = DayQuality(session_date=session_date, instrument_symbol=instrument_symbol,
                   passed=True, bar_count=len(bars))
    if not bars:
        q.passed = False
        q.reasons = ["no bars"]
        return q

    q.total_volume = sum(b.volume for b in bars)

    if len(bars) < thresholds.min_bars:
        reasons.append(f"only {len(bars)} bars (min {thresholds.min_bars})")
    missing = Decimal(max(0, expected_bars - len(bars))) / Decimal(expected_bars)
    if missing > thresholds.max_missing_fraction:
        reasons.append(f"{missing:.1%} of expected bars missing")
    if q.total_volume < thresholds.min_total_volume:
        reasons.append(f"total volume {q.total_volume} below "
                       f"{thresholds.min_total_volume}")
    zero_vol = sum(1 for b in bars if b.volume == 0)
    zero_frac = Decimal(zero_vol) / Decimal(len(bars))
    if zero_frac > thresholds.max_zero_volume_fraction:
        reasons.append(f"{zero_frac:.1%} of bars have zero volume")

    # Bad prints: a single bar whose range dwarfs the day's typical range.
    ranges = sorted((b.high - b.low) for b in bars)
    median_range = ranges[len(ranges) // 2]
    if median_range > 0:
        worst = max(ranges)
        if worst / median_range > thresholds.max_single_bar_move_atr:
            reasons.append(
                f"a single bar's range is {worst / median_range:.1f}x the "
                f"median; likely a bad print or a halt reopen")

    # Duplicate or out-of-order timestamps would already have been refused
    # upstream, but a day assembled from files can still contain them.
    times = [b.observed_at for b in bars]
    if len(set(times)) != len(times):
        reasons.append("duplicate timestamps")
    if times != sorted(times):
        reasons.append("bars out of chronological order")

    q.reasons = reasons
    q.passed = not reasons
    return q


@dataclass
class QualityReport:
    """The denominator, stated honestly."""
    assessed: List[DayQuality] = field(default_factory=list)

    def add(self, day: DayQuality) -> None:
        self.assessed.append(day)

    @property
    def passed_days(self) -> List[date]:
        return sorted(d.session_date for d in self.assessed if d.passed)

    @property
    def excluded_days(self) -> List[DayQuality]:
        return [d for d in self.assessed if not d.passed]

    def summary(self) -> dict:
        counts: Dict[str, int] = {}
        for d in self.excluded_days:
            for r in d.reasons:
                key = r.split("(")[0].split(";")[0].strip()
                key = "".join(c for c in key if not c.isdigit()).strip()
                counts[key] = counts.get(key, 0) + 1
        return {
            "days_assessed": len(self.assessed),
            "days_passed": len(self.passed_days),
            "days_excluded": len(self.excluded_days),
            "exclusion_reasons": dict(sorted(counts.items())),
        }
