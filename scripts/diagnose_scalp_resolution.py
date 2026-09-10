#!/usr/bin/env python3
"""Can 1-minute bars resolve a seconds-to-minutes scalp? A diagnostic.

NOT RESEARCH. This measures the DATA, not the market, and answers one
question: is a 5-15 point move completed within a minute visible at all
in one-minute OHLCV, or does it happen inside a bar where the ordering
of high and low is unknowable?

WHY IT MATTERS MORE THAN ANY RESULT SO FAR. Every horizon tested to
date is 15 to 60 minutes. A trade held for seconds to a minute lives
inside a single bar of this dataset. If most one-minute bars already
span the trade's whole target, then bar data cannot say whether the
favourable level or the adverse one came first -- and a study built on
it would be answering with a coin flip while reporting a number.

TWO MEASUREMENTS

  1. The distribution of one-minute RTH bar ranges. If the median bar
     spans more than the target, the target is an intrabar event.
  2. For symmetric brackets, how often BOTH sides are touched within
     one bar. That is the `same_bar` outcome the path scanner already
     refuses to adjudicate, and its frequency is the resolution limit
     expressed directly.

Discovery partition only. Reads no validation and no holdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.features.config import FeatureConfig
from trading_system.features.engine import FeatureEngine
from trading_system.market_data import Instrument
from trading_system.research.hypotheses import Direction
from trading_system.research.outcomes import BarWindowIndex, measure_forward_path
from trading_system.research.partitions import Partition, split_chronologically
from trading_system.research.progress import StageProgress
from trading_system.research.quality import QualityReport, assess_day
from trading_system.sources.databento_source import DatabentoFileSource

INSTRUMENTS = {
    "NQ.c.0": Instrument(symbol="NQ.c.0", product="NQ", tick_size=Decimal("0.25")),
    "MNQ.c.0": Instrument(symbol="MNQ.c.0", product="MNQ", tick_size=Decimal("0.25")),
    "ES.c.0": Instrument(symbol="ES.c.0", product="ES", tick_size=Decimal("0.25")),
}

BRACKETS = [Decimal(x) for x in ("2.5", "5", "7.5", "10", "15", "20", "30")]
HORIZON_MINUTES = 5
SAMPLE_STRIDE = 7          # every 7th RTH bar; deterministic, ample


def verify_manifest(data_dir: Path) -> dict:
    manifest = json.loads((data_dir / "manifest.json").read_text())
    dbn = data_dir / manifest["file"]
    actual = hashlib.sha256(dbn.read_bytes()).hexdigest()
    if actual != manifest["sha256"]:
        raise SystemExit(f"INTEGRITY FAILURE on {manifest['file']}")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data/t004")
    ap.add_argument("--symbol", default="NQ.c.0", choices=sorted(INSTRUMENTS))
    ap.add_argument("--out", default="scalp_resolution.json")
    args = ap.parse_args()

    print("SCALP RESOLUTION DIAGNOSTIC")
    print("Measures the DATA, not the market. Discovery partition only.\n")

    data_dir = Path(args.data_dir)
    manifest = verify_manifest(data_dir)
    instrument = INSTRUMENTS[args.symbol]
    source = DatabentoFileSource(data_dir / manifest["file"], INSTRUMENTS,
                                 datetime.fromisoformat(manifest["finished_at"]))
    print("1. manifest verified; resolving symbology ...")
    bars = source.bars_by_symbol()[instrument.symbol]
    config = FeatureConfig()
    calendar = FeatureEngine(instrument, config).calendar

    by_day = {}
    for b in bars:
        by_day.setdefault(calendar.session_date_for(b.observed_at), []).append(b)
    quality = QualityReport()
    for day, day_bars in sorted(by_day.items()):
        quality.add(assess_day(day, instrument.symbol, day_bars, calendar))
    usable = quality.passed_days
    days = split_chronologically(usable).select(usable, Partition.DISCOVERY)
    print(f"2. discovery ONLY: {len(days)} sessions "
          f"({days[0]} .. {days[-1]})\n")

    rth = []
    for day in days:
        opened = calendar.rth_open_at(day)
        closed = calendar.rth_close_at(day)
        rth.extend(b for b in by_day[day] if opened <= b.observed_at < closed)
    ranges = sorted(float(b.high - b.low) for b in rth)
    print(f"3. ONE-MINUTE RTH BAR RANGES  ({len(ranges):,} bars)\n")
    for pct in (10, 25, 50, 75, 90, 99):
        value = ranges[min(len(ranges) - 1, int(len(ranges) * pct / 100))]
        print(f"   {pct:>3}th percentile   {value:>8.2f} points")
    print(f"   mean              {statistics.mean(ranges):>8.2f} points\n")
    print("   fraction of bars whose range already spans a target of:")
    for target in BRACKETS:
        t = float(target)
        share = sum(1 for r in ranges if r >= t) / len(ranges)
        note = "  <-- your target range" if 5 <= t <= 15 else ""
        print(f"     {t:>5.1f} points   {share:>6.1%}{note}")

    print(f"\n4. BRACKET RESOLUTION over {HORIZON_MINUTES} minutes")
    print("   Of the times a symmetric bracket is resolved at all, how often")
    print("   BOTH sides are hit inside ONE bar -- where this data cannot say")
    print("   which came first.\n")
    sample = rth[::SAMPLE_STRIDE]
    index = BarWindowIndex(bars)
    print(f"   {'bracket':>9} {'resolved':>9} {'favorable':>10} "
          f"{'adverse':>9} {'same bar':>10} {'unresolvable':>13}")
    print("   " + "-" * 64)
    rows = []
    progress = StageProgress("resolution", len(BRACKETS), unit="brackets",
                             min_interval=0.0, counter_labels=("bars", "hits"))
    for target in BRACKETS:
        counts = {"favorable": 0, "adverse": 0, "same_bar": 0, "neither": 0}
        for bar in sample:
            measured = measure_forward_path("probe", bar.closed_at, bars,
                                            HORIZON_MINUTES, index)
            if measured is None:
                continue
            _path, scanner = measured
            if not scanner.bars:
                continue
            outcome, _when = scanner.hit_first(Direction.UP, target, target)
            counts[outcome] += 1
        decided = counts["favorable"] + counts["adverse"] + counts["same_bar"]
        share = (counts["same_bar"] / decided) if decided else 0.0
        rows.append({"bracket_points": str(target), **counts,
                     "same_bar_share_of_decided": share})
        total = sum(counts.values()) or 1
        print(f"   {float(target):>8.1f}p {decided / total:>8.1%} "
              f"{counts['favorable'] / total:>9.1%} "
              f"{counts['adverse'] / total:>8.1%} "
              f"{share:>9.1%} {counts['neither'] / total:>12.1%}")
        progress.advance(len(sample), decided)
    progress.finish()

    print("\n5. READING THIS")
    print("   'same bar' is the fraction of resolved cases where the")
    print("   favourable and adverse levels were BOTH touched within one")
    print("   minute. For those, one-minute data cannot say which came")
    print("   first, so any study of a bracket that size is guessing on")
    print("   that share of its sample. A high number at your bracket means")
    print("   finer data is a precondition for the research, not an upgrade.")

    report = {
        "diagnostic": "scalp_resolution", "partition_used": "discovery",
        "validation_read": False, "holdout_read": False,
        "symbol": args.symbol, "sessions": len(days),
        "rth_bars": len(ranges), "sample_stride": SAMPLE_STRIDE,
        "horizon_minutes": HORIZON_MINUTES,
        "bar_range_percentiles": {
            str(p): ranges[min(len(ranges) - 1, int(len(ranges) * p / 100))]
            for p in (10, 25, 50, 75, 90, 99)},
        "bar_range_mean": statistics.mean(ranges),
        "brackets": rows,
        "manifest_sha256": manifest["sha256"],
    }
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True,
                                         default=str))
    print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
