#!/usr/bin/env python3
"""Replay demonstration and benchmark for the T-003 feature engine.

Runs the engine twice over a generated session and proves the three
properties that matter, then reports throughput.

  DETERMINISM     two runs produce byte-identical output
  NO LOOKAHEAD    a truncated run equals the prefix of the full run
  PROVENANCE      every record carries engine version and config digest

No network, no database, no credentials. Deterministic input: the
"random walk" is a fixed arithmetic recurrence, so the numbers are the
same on every machine and in every CI run.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.market_data import Bar, Instrument
from trading_system.features.config import FeatureConfig
from trading_system.features.engine import ENGINE_VERSION, FeatureEngine

NQ = Instrument(symbol="NQZ6", product="NQ", tick_size=Decimal("0.25"))
RTH_OPEN = datetime(2026, 1, 12, 14, 30, tzinfo=timezone.utc)


def generate(minutes: int):
    """A deterministic pseudo-walk on the 0.25 tick grid.

    Not random: a fixed recurrence, so the benchmark measures the engine
    rather than the generator, and any two runs anywhere agree.
    """
    bars = []
    price = Decimal("20000.00")
    state = 7
    for i in range(minutes):
        state = (state * 1103515245 + 12345) % 2147483648
        step = Decimal((state % 17) - 8) * Decimal("0.25")
        open_ = price
        close = open_ + step
        high = max(open_, close) + Decimal(state % 5) * Decimal("0.25")
        low = min(open_, close) - Decimal((state >> 3) % 5) * Decimal("0.25")
        volume = 50 + (state % 500)
        t = RTH_OPEN + timedelta(minutes=i)
        bars.append(Bar(instrument=NQ, interval_seconds=60, open=open_, high=high,
                        low=low, close=close, volume=volume, observed_at=t,
                        captured_at=t + timedelta(milliseconds=40),
                        provider="replay-demo"))
        price = close
    return bars


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=390)  # one RTH session
    args = ap.parse_args()

    bars = generate(args.minutes)
    config = FeatureConfig()
    print(f"engine {ENGINE_VERSION}   config {config.version_label()}")
    print(f"input  {len(bars)} one-minute bars\n")

    start = time.perf_counter()
    first = FeatureEngine(NQ, config, run_id="demo").run(bars)
    elapsed = time.perf_counter() - start
    second = FeatureEngine(NQ, config, run_id="demo").run(bars)

    rows_a = [r.as_row() for r in first]
    rows_b = [r.as_row() for r in second]
    deterministic = rows_a == rows_b

    cut = len(bars) // 2
    partial = FeatureEngine(NQ, config, run_id="demo").run(bars[:cut])
    cutoff = bars[cut - 1].closed_at
    expected = [r.as_row() for r in first if r.available_at <= cutoff]
    no_lookahead = [r.as_row() for r in partial] == expected

    provenance = all(r.engine_version == ENGINE_VERSION
                     and r.config_digest == config.digest()
                     for r in first)

    features = sum(1 for r in first if r.kind.value == "feature")
    events = sum(1 for r in first if r.kind.value == "event")
    delayed = [r for r in first if r.confirmation_lag.total_seconds() > 0]

    print(f"records            {len(first)}  ({features} features, {events} events)")
    print(f"delayed-availability records  {len(delayed)}")
    by_type = {}
    for r in first:
        by_type[r.type] = by_type.get(r.type, 0) + 1
    print("\nevent counts:")
    for t, n in sorted(by_type.items()):
        if any(r.kind.value == "event" for r in first if r.type == t):
            print(f"  {t:32} {n}")

    print(f"\ndeterministic          {'PASS' if deterministic else 'FAIL'}")
    print(f"no lookahead (prefix)  {'PASS' if no_lookahead else 'FAIL'}")
    print(f"provenance complete    {'PASS' if provenance else 'FAIL'}")

    per_bar_us = elapsed / len(bars) * 1_000_000
    print(f"\nbenchmark: {elapsed*1000:.1f} ms for {len(bars)} bars "
          f"({per_bar_us:.1f} us/bar, {len(bars)/elapsed:,.0f} bars/s)")
    print(f"           {len(first)/elapsed:,.0f} records/s")
    est = 390 * 252
    print(f"           one instrument-year of RTH minutes (~{est:,} bars) "
          f"in ~{est/(len(bars)/elapsed):.1f} s")

    return 0 if (deterministic and no_lookahead and provenance) else 1


if __name__ == "__main__":
    raise SystemExit(main())
