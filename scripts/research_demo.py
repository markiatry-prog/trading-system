#!/usr/bin/env python3
"""End-to-end Phase A research demonstration on synthetic data.

THE POINT OF THIS DEMO IS THAT IT SHOULD FIND NOTHING.

The bars come from a deterministic recurrence with no embedded edge. A
framework that reports ROBUST findings here is broken -- it is
manufacturing signal from noise, which is exactly the failure the whole
apparatus exists to prevent. The expected verdicts are REJECT and
INCONCLUSIVE, and the demo FAILS if anything is classified ROBUST.

That is a stronger check than asserting the plumbing runs.

No network, no database, no credentials, no paid data.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.features.config import FeatureConfig
from trading_system.features.engine import FeatureEngine
from trading_system.market_data import Bar, Instrument
from trading_system.research.classify import Verdict
from trading_system.research.hypotheses import Direction, HypothesisRegistry
from trading_system.research.partitions import Partition, split_chronologically
from trading_system.research.quality import QualityReport, assess_day
from trading_system.research.study import Study

NQ = Instrument(symbol="NQZ6", product="NQ", tick_size=Decimal("0.25"))


def session_bars(day_index: int, minutes: int = 390):
    """Deterministic pseudo-walk. No edge is embedded, by construction."""
    open_utc = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc) + timedelta(days=day_index)
    bars = []
    price = Decimal(20000 + day_index)
    state = 7 + day_index * 31
    for i in range(minutes):
        state = (state * 1103515245 + 12345) % 2147483648
        step = Decimal((state % 17) - 8) * Decimal("0.25")
        o = price
        c = o + step
        h = max(o, c) + Decimal(state % 5) * Decimal("0.25")
        l = min(o, c) - Decimal((state >> 3) % 5) * Decimal("0.25")
        t = open_utc + timedelta(minutes=i)
        bars.append(Bar(instrument=NQ, interval_seconds=60, open=o, high=h, low=l,
                        close=c, volume=50 + (state % 500), observed_at=t,
                        captured_at=t + timedelta(milliseconds=40),
                        provider="research-demo"))
        price = c
    return open_utc.date(), bars


def main() -> int:
    n_days = 40
    print("Phase A research framework -- synthetic data, no embedded edge\n")

    # 1. build sessions and gate quality BEFORE anything is measured
    quality = QualityReport()
    per_day = {}
    for d in range(n_days):
        if (datetime(2026, 1, 5) + timedelta(days=d)).weekday() >= 5:
            continue
        day, bars = session_bars(d)
        quality.add(assess_day(day, "NQZ6", bars))
        per_day[day] = bars
    q = quality.summary()
    print(f"1. data quality: {q['days_passed']}/{q['days_assessed']} days passed, "
          f"{q['days_excluded']} excluded")

    usable = quality.passed_days
    if len(usable) < 6:
        print("not enough usable days"); return 1

    # 2. chronological partitions, holdout sealed
    parts = split_chronologically(usable)
    print(f"2. partitions: discovery {parts.discovery[0]}..{parts.discovery[1]}, "
          f"validation {parts.validation[0]}..{parts.validation[1]}, "
          f"holdout SEALED ({parts.holdout[0]}..{parts.holdout[1]})")

    # 3. pre-register BEFORE any test runs
    reg = HypothesisRegistry()
    reg.register("H1", "An ORB high break is followed by continuation up",
                 "opening_range_high_broken", Direction.UP, 30, {},
                 "mean forward return CI includes zero, or is negative")
    reg.register("H2", "An ORB high break above VWAP continues more reliably",
                 "opening_range_high_broken", Direction.UP, 30,
                 {"above_vwap": "above"},
                 "no difference from the unconditional case; CI includes zero")
    reg.register("H3", "Displacement up is followed by further upside",
                 "displacement_up", Direction.UP, 15, {},
                 "mean forward return CI includes zero")
    reg.register("H4", "A structure break down is followed by continuation down",
                 "structure_break_down", Direction.DOWN, 30, {},
                 "mean signed return CI includes zero")
    reg.seal()
    print(f"3. pre-registered {reg.count()} hypotheses, chain valid="
          f"{reg.verify()}, registry SEALED")

    study = Study("phase-a-demo", parts, reg, quality)

    # 4. run discovery and validation. The holdout is NOT touched.
    for partition in (Partition.DISCOVERY, Partition.VALIDATION):
        days = parts.select(usable, partition)
        events, features, bars = [], [], []
        for day in days:
            day_bars = per_day[day]
            recs = FeatureEngine(NQ, FeatureConfig()).run(day_bars)
            events.extend(r for r in recs if r.kind.value == "event")
            features.extend(r for r in recs if r.kind.value == "feature")
            bars.extend(day_bars)
        for h in reg.all():
            study.test(h.id, partition, events, features, bars)
        print(f"4. {partition.value}: {len(days)} days, {len(events)} events")

    # 5. classify with multiplicity correction
    print("\n5. verdicts (multiplicity-corrected):")
    verdicts = study.classify_all()
    robust = []
    for hid, c in sorted(verdicts.items()):
        res = next((r for r in study.results
                    if r.hypothesis_id == hid and r.partition == "discovery"), None)
        n = res.estimate.n if res and res.estimate else 0
        print(f"   {hid}  {c.verdict.value.upper():13} n={n:<5} {c.reason[:88]}")
        if c.verdict is Verdict.ROBUST:
            robust.append(hid)

    print(f"\n6. tests actually run (snooping ledger): "
          f"{study.ledger.total_tests}")
    print(f"7. holdout ever unsealed: "
          f"{study.provenance()['partitions']['holdout_ever_unsealed']}")
    print(f"8. reproducibility digest: {study.reproducibility_digest()[:32]}")

    if robust:
        print(f"\nFAIL: {robust} classified ROBUST on data with no embedded "
              f"edge. The framework is manufacturing findings.")
        return 1
    print("\nPASS: nothing classified ROBUST on edgeless data, as required.")
    print("      Null and inconclusive results are the correct outcome here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
