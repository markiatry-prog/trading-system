#!/usr/bin/env python3
"""Audit the data-quality gate against the real dataset.

Answers, from the acquired file and nothing else:
  - which rule rejected each session, under the OLD gate and the NEW one
  - the distribution of RTH coverage and of missing-bar percentages
  - whether missing minutes concentrate overnight or in liquid hours
  - whether exclusions cluster by year, weekday, holiday/early close,
    contract roll, or realised volatility
  - whether ohlcv-1m emits bars only when trades occur
  - whether a fixed expected_bars of 1380 was ever appropriate
  - whether the gate biases the sample toward active sessions

Also exports an ANONYMISED fixture: per-session counts, coverage and
gap statistics with no prices, so the gate can be regression-tested
against real behaviour without the dataset leaving your machine.

Read-only. Touches neither the dataset nor the manifest.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.features.calendar import (
    SessionCalendar, early_close_time, is_holiday)
from trading_system.features.config import FeatureConfig
from trading_system.market_data import Instrument
from trading_system.research.quality import (
    QualityReport, QualityThresholds, assess_day, expected_rth_minutes,
    implausible_prints, longest_missing_run)
from trading_system.sources.databento_source import DatabentoFileSource

INSTRUMENTS = {
    "NQ.c.0": Instrument(symbol="NQ.c.0", product="NQ", tick_size=Decimal("0.25")),
    "MNQ.c.0": Instrument(symbol="MNQ.c.0", product="MNQ", tick_size=Decimal("0.25")),
    "ES.c.0": Instrument(symbol="ES.c.0", product="ES", tick_size=Decimal("0.25")),
}
OLD_EXPECTED_BARS = 1380


def old_gate(bars, expected_bars=OLD_EXPECTED_BARS):
    """The gate as it was, reproduced exactly, so before/after is real."""
    reasons = []
    if not bars:
        return ["no bars"]
    if len(bars) < 300:
        reasons.append("min_bars")
    missing = max(0, expected_bars - len(bars)) / expected_bars
    if missing > 0.10:
        reasons.append("max_missing_fraction")
    if sum(b.volume for b in bars) < 1000:
        reasons.append("min_total_volume")
    zero = sum(1 for b in bars if b.volume == 0) / len(bars)
    if zero > 0.30:
        reasons.append("max_zero_volume_fraction")
    ranges = sorted((b.high - b.low) for b in bars)
    median = ranges[len(ranges) // 2]
    if median > 0 and ranges[-1] / median > 10:
        reasons.append("max_single_bar_move_atr")
    times = [b.observed_at for b in bars]
    if len(set(times)) != len(times):
        reasons.append("duplicate_timestamps")
    if times != sorted(times):
        reasons.append("out_of_order")
    return reasons


def histogram(values, edges, label):
    print(f"\n{label}")
    counts = Counter()
    for v in values:
        placed = False
        for lo, hi in zip(edges, edges[1:]):
            if lo <= v < hi:
                counts[(lo, hi)] += 1
                placed = True
                break
        if not placed:
            counts[(edges[-1], None)] += 1
    for lo, hi in list(zip(edges, edges[1:])) + [(edges[-1], None)]:
        n = counts[(lo, hi)]
        if n == 0:
            continue
        rng = f"{lo:6.2f} - {hi:6.2f}" if hi is not None else f"{lo:6.2f} +     "
        print(f"  {rng}  {n:>6}  {'#' * min(60, n * 60 // max(1, len(values)))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/t004")
    ap.add_argument("--symbol", default="NQ.c.0")
    ap.add_argument("--fixture-out", default="quality_fixture.json")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    manifest = json.loads((data_dir / "manifest.json").read_text())
    instrument = INSTRUMENTS[args.symbol]
    source = DatabentoFileSource(data_dir / manifest["file"], INSTRUMENTS,
                                 datetime.now(timezone.utc))
    print(f"loading {args.symbol} ...")
    bars = source.bars_by_symbol()[args.symbol]
    print(f"{len(bars):,} bars\n")

    calendar = SessionCalendar(FeatureConfig().session)
    by_day = defaultdict(list)
    for b in bars:
        by_day[calendar.session_date_for(b.observed_at)].append(b)
    days = sorted(by_day)
    print(f"{len(days)} sessions, {days[0]} .. {days[-1]}")

    # -- is ohlcv-1m sparse? ------------------------------------------
    print("\n" + "=" * 66)
    print("DOES ohlcv-1m EMIT A BAR FOR EVERY MINUTE, OR ONLY ON TRADES?")
    zero_vol = sum(1 for b in bars if b.volume == 0)
    print(f"  bars with volume == 0 : {zero_vol:,} of {len(bars):,}")
    print("  -> zero means the schema emits a bar ONLY when trades occur,")
    print("     so absent minutes are silence, not missing data.")
    sample = days[len(days) // 2]
    sample_bars = by_day[sample]
    opened = calendar.rth_open_at(sample)
    closed = calendar.rth_close_at(sample)
    rth = [b for b in sample_bars if opened <= b.observed_at < closed]
    on = [b for b in sample_bars if not (opened <= b.observed_at < closed)]
    exp_rth = expected_rth_minutes(calendar, sample)
    print(f"\n  sample session {sample}: {len(sample_bars):,} bars total")
    print(f"    RTH       {len(rth):>5} bars / {exp_rth} minutes "
          f"= {len(rth)/max(1,exp_rth):6.1%} coverage")
    print(f"    overnight {len(on):>5} bars")

    counts = [len(by_day[d]) for d in days]
    print(f"\n  bars per session: min {min(counts):,}  median "
          f"{statistics.median(counts):,.0f}  mean {statistics.fmean(counts):,.0f}"
          f"  max {max(counts):,}")
    over = sum(1 for c in counts if c > OLD_EXPECTED_BARS)
    print(f"  sessions with MORE than the assumed {OLD_EXPECTED_BARS} bars: "
          f"{over} ({over/len(days):.1%})")
    print("  -> a fixed expected_bars cannot describe this; the window")
    print("     varies with DST, early closes and the Sunday open.")

    # -- old gate vs new gate -----------------------------------------
    print("\n" + "=" * 66)
    print("BEFORE / AFTER")
    old_reasons = Counter()
    old_failed = set()
    for d in days:
        r = old_gate(by_day[d])
        if r:
            old_failed.add(d)
            for reason in r:
                old_reasons[reason] += 1

    report = QualityReport()
    for d in days:
        report.add(assess_day(d, args.symbol, by_day[d], calendar))
    new = report.summary()

    print(f"\n  OLD gate: {len(days) - len(old_failed)}/{len(days)} passed, "
          f"{len(old_failed)} excluded")
    for reason, n in old_reasons.most_common():
        print(f"      {n:>6}  {reason}")
    print(f"\n  NEW gate: {new['days_passed']}/{new['days_assessed']} passed, "
          f"{new['days_excluded']} excluded")
    for reason, n in sorted(new["exclusion_reasons"].items(),
                            key=lambda kv: -kv[1]):
        print(f"      {n:>6}  {reason}")
    print("\n  observations (recorded, never excluding):")
    for obs, n in sorted(new["observations"].items(), key=lambda kv: -kv[1]):
        print(f"      {n:>6}  {obs}")

    # -- distributions -------------------------------------------------
    per_day = {d: assess_day(d, args.symbol, by_day[d], calendar) for d in days}
    coverage = [per_day[d].rth_coverage for d in days]
    histogram(coverage, [0, .5, .8, .9, .95, .98, .99, 1.0],
              "RTH COVERAGE DISTRIBUTION")
    old_missing = [max(0, OLD_EXPECTED_BARS - len(by_day[d])) / OLD_EXPECTED_BARS
                   for d in days]
    histogram(old_missing, [0, .05, .10, .20, .40, .60],
              "OLD missing-bar fraction (against the fixed 1380)")
    ratios = [per_day[d].max_range_ratio for d in days]
    histogram(ratios, [0, 5, 10, 20, 40, 80, 160],
              "MAX/MEDIAN BAR-RANGE RATIO (what the old gate excluded on)")

    # -- clustering ----------------------------------------------------
    print("\n" + "=" * 66)
    print("DO EXCLUSIONS CLUSTER?")
    def cluster(keyfn, label):
        tot, exc = Counter(), Counter()
        for d in days:
            k = keyfn(d)
            tot[k] += 1
            if not per_day[d].passed:
                exc[k] += 1
        print(f"\n  by {label}:")
        for k in sorted(tot):
            rate = exc[k] / tot[k]
            print(f"    {str(k):<12} {exc[k]:>5}/{tot[k]:<5} {rate:6.1%} "
                  f"{'*' if rate > 0.2 else ''}")

    cluster(lambda d: d.year, "year")
    cluster(lambda d: d.strftime("%a"), "weekday")
    cluster(lambda d: "early-close" if early_close_time(d) else "normal",
            "early close")
    cluster(lambda d: "quarter-end" if d.month in (3, 6, 9, 12) and d.day > 7
            else "other", "roll window (3rd week of quarter months)")

    passed = [d for d in days if per_day[d].passed]
    excluded = [d for d in days if not per_day[d].passed]
    def realised(d):
        b = by_day[d]
        return float(max(x.high for x in b) - min(x.low for x in b)) if b else 0.0
    if passed and excluded:
        print(f"\n  SELECTION BIAS CHECK (realised session range):")
        print(f"    passed   n={len(passed):<5} median {statistics.median(map(realised, passed)):,.1f}")
        print(f"    excluded n={len(excluded):<5} median {statistics.median(map(realised, excluded)):,.1f}")
        print("    -> large divergence would mean the gate selects on volatility")

    # -- fixture -------------------------------------------------------
    # Contract provenance, anonymised. The rule only ever asks whether
    # two adjacent sessions carry the SAME contract, so a stable label
    # in first-seen order preserves everything it needs while keeping
    # the fixture free of anything but statistics. The real ids stay in
    # the dataset.
    contract_label = {}
    def labels_for(d):
        out = []
        for b in by_day[d]:
            cid = getattr(b, "contract_id", None)
            if cid is None:
                if None not in contract_label:
                    contract_label[None] = "UNKNOWN"
                lbl = "UNKNOWN"
            else:
                if cid not in contract_label:
                    contract_label[cid] = f"C{len(contract_label)}"
                lbl = contract_label[cid]
            if lbl not in out:
                out.append(lbl)
        return sorted(out)

    session_contracts = {d: labels_for(d) for d in days}

    fixture = {
        "symbol": args.symbol,
        "source_sha256": manifest["sha256"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "anonymised per-session statistics; contains no prices. "
                "`contracts` are stable labels in first-seen order, not "
                "exchange ids: the rule only compares them for equality.",
        "sessions": [
            {
                "date": d.isoformat(),
                "bars": len(by_day[d]),
                "rth_bars": per_day[d].rth_bar_count,
                "overnight_bars": per_day[d].overnight_bar_count,
                "expected_rth_minutes": per_day[d].expected_rth_minutes,
                "rth_coverage": round(per_day[d].rth_coverage, 4),
                "longest_rth_gap": per_day[d].longest_rth_gap,
                "max_range_ratio": round(per_day[d].max_range_ratio, 2),
                "zero_volume_bars": sum(1 for b in by_day[d] if b.volume == 0),
                "old_gate_reasons": old_gate(by_day[d]),
                "new_gate_exclusions": per_day[d].exclusions,
                "new_gate_observations": per_day[d].observations,
                "contracts": session_contracts[d],
            }
            for d in days
        ],
    }
    Path(args.fixture_out).write_text(json.dumps(fixture, indent=1))
    print(f"\nanonymised fixture written to {args.fixture_out} "
          f"({len(fixture['sessions'])} sessions, no prices)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
