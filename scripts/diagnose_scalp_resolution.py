#!/usr/bin/env python3
"""Can 1-minute bars resolve a seconds-to-minutes scalp? A diagnostic.

NOT RESEARCH, AND NOT A SELECTION. This measures the DATA, not the
market. It answers one question -- which bracket and horizon
combinations one-minute OHLCV can actually adjudicate -- and it must not
be used to pick the bracket or horizon that shows the nicest
directional result. Doing so would choose a research parameter from an
outcome, which is the mining this whole framework exists to prevent.
The directional split is printed because it is the null any future
edge must beat, not because the largest one is interesting.

THE TWO WAYS THIS DATA CAN FAIL, KEPT SEPARATE

  AMBIGUITY     both sides of the bracket touched inside ONE bar. The
                order is then unknowable from OHLCV, and a study of
                that bracket is guessing on that share of its sample.
                This is a RESOLUTION limit -- finer data would fix it.
  REACHABILITY  neither side touched before the horizon expires. That
                is a fact about the market, not the data: a finer feed
                would report the same non-event. A bracket can be
                perfectly measurable and still rarely reached.

Conflating them would let a market fact masquerade as a data problem,
or the reverse.

FIRST TOUCH IS REPORTED TWICE

  conservative  same-bar counted as a failure. In live trading a minute
                that trades through both levels is a minute you were
                probably stopped, so this is the honest default.
  diagnostic    same-bar excluded. Shows what the resolvable subset did,
                and by how much the ambiguity is flattering the result.

THE SESSION IS SPLIT, because the operator trades one window

  A  09:30-10:50 ET   the first 80 minutes of RTH, taken from the
                      calendar's own open so it stays correct across
                      daylight saving
  B  rest of RTH

Discovery partition only. Reads no validation and no holdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from datetime import datetime, timedelta
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

BRACKETS = [Decimal(x) for x in ("5", "7.5", "10", "15", "20")]
HORIZONS = (1, 2, 3, 5)
OPENING_WINDOW_MINUTES = 80          # 09:30 -> 10:50 ET
SAMPLE_STRIDE = 3

# Declared before the run, so a verdict is a rule rather than a reading.
MAX_AMBIGUITY_SHARE = 0.05           # above this, resolution-limited
LOW_REACHABILITY_SHARE = 0.25        # below this, rarely reached


def verify_manifest(data_dir: Path) -> dict:
    manifest = json.loads((data_dir / "manifest.json").read_text())
    actual = hashlib.sha256((data_dir / manifest["file"]).read_bytes()).hexdigest()
    if actual != manifest["sha256"]:
        raise SystemExit(f"INTEGRITY FAILURE on {manifest['file']}")
    return manifest


def percentiles(values, points=(10, 25, 50, 75, 90, 99)):
    ordered = sorted(values)
    return {str(p): ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))]
            for p in points}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data/t004")
    ap.add_argument("--symbol", default="NQ.c.0", choices=sorted(INSTRUMENTS))
    ap.add_argument("--out", default="scalp_resolution.json")
    args = ap.parse_args()

    print("SCALP RESOLUTION AND REACHABILITY DIAGNOSTIC")
    print("Measures the DATA, not the market. Discovery partition only.")
    print("Not a selection: do NOT pick a bracket or horizon from the")
    print("directional columns below.\n")

    data_dir = Path(args.data_dir)
    manifest = verify_manifest(data_dir)
    instrument = INSTRUMENTS[args.symbol]
    source = DatabentoFileSource(data_dir / manifest["file"], INSTRUMENTS,
                                 datetime.fromisoformat(manifest["finished_at"]))
    print("1. manifest verified; resolving symbology ...")
    bars = source.bars_by_symbol()[instrument.symbol]
    calendar = FeatureEngine(instrument, FeatureConfig()).calendar

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

    # -- split the session ------------------------------------------
    windows = {"A": [], "B": []}
    for day in days:
        opened = calendar.rth_open_at(day)
        closed = calendar.rth_close_at(day)
        cutoff = opened + timedelta(minutes=OPENING_WINDOW_MINUTES)
        for b in by_day[day]:
            if opened <= b.observed_at < cutoff:
                windows["A"].append(b)
            elif cutoff <= b.observed_at < closed:
                windows["B"].append(b)

    labels = {"A": "09:30-10:50 ET (the traded window)",
              "B": "rest of RTH"}
    print("3. ONE-MINUTE BAR RANGES BY WINDOW\n")
    ranges = {}
    for key in ("A", "B"):
        ranges[key] = [float(b.high - b.low) for b in windows[key]]
        pcts = percentiles(ranges[key])
        print(f"   {key}  {labels[key]}   ({len(ranges[key]):,} bars)")
        print("      " + "  ".join(f"p{p}={pcts[p]:.2f}" for p in
                                   ("10", "25", "50", "75", "90", "99")))
        print(f"      mean {statistics.mean(ranges[key]):.2f} points\n")

    # -- the sweep ---------------------------------------------------
    index = BarWindowIndex(bars)
    rows = []
    total_steps = len(windows) * len(HORIZONS)
    progress = StageProgress("sweep", total_steps, unit="window-horizons",
                             min_interval=0.0, counter_labels=("anchors", "hits"))
    for key in ("A", "B"):
        sample = windows[key][::SAMPLE_STRIDE]
        for horizon in HORIZONS:
            counts = {str(b): {"favorable": 0, "adverse": 0, "same_bar": 0,
                               "neither": 0} for b in BRACKETS}
            measured_any = 0
            for bar in sample:
                measured = measure_forward_path("probe", bar.closed_at, bars,
                                                horizon, index)
                if measured is None:
                    continue
                _path, scanner = measured
                if not scanner.bars:
                    continue
                measured_any += 1
                for bracket in BRACKETS:
                    outcome, _when = scanner.hit_first(Direction.UP, bracket,
                                                       bracket)
                    counts[str(bracket)][outcome] += 1
            for bracket in BRACKETS:
                c = counts[str(bracket)]
                anchors = c["favorable"] + c["adverse"] + c["same_bar"] + c["neither"]
                decided = c["favorable"] + c["adverse"] + c["same_bar"]
                clean = c["favorable"] + c["adverse"]
                rows.append({
                    "window": key, "window_label": labels[key],
                    "horizon_minutes": horizon, "bracket_points": str(bracket),
                    "anchors": anchors, **c,
                    "resolved": decided,
                    "resolved_rate": decided / anchors if anchors else 0.0,
                    "unresolved_rate": c["neither"] / anchors if anchors else 0.0,
                    "favorable_rate": c["favorable"] / anchors if anchors else 0.0,
                    "adverse_rate": c["adverse"] / anchors if anchors else 0.0,
                    "same_bar_rate": c["same_bar"] / anchors if anchors else 0.0,
                    "ambiguity_share_of_resolved":
                        c["same_bar"] / decided if decided else 0.0,
                    # same-bar counts as a failure
                    "first_touch_conservative":
                        c["favorable"] / decided if decided else None,
                    # same-bar excluded
                    "first_touch_diagnostic":
                        c["favorable"] / clean if clean else None,
                })
            progress.advance(len(sample), measured_any)
    progress.finish()

    # -- report ------------------------------------------------------
    for key in ("A", "B"):
        print(f"\n4{'A' if key == 'A' else 'B'}. WINDOW {key} -- {labels[key]}\n")
        print(f"   {'h':>2} {'brkt':>5} {'anchors':>8} {'resolvd':>8} "
              f"{'fav':>7} {'adv':>7} {'same':>7} {'unres':>7} "
              f"{'ambig':>7} {'FT cons':>8} {'FT diag':>8}  verdict")
        print("   " + "-" * 104)
        for horizon in HORIZONS:
            for r in [x for x in rows if x["window"] == key
                      and x["horizon_minutes"] == horizon]:
                ambiguous = r["ambiguity_share_of_resolved"] > MAX_AMBIGUITY_SHARE
                rare = r["resolved_rate"] < LOW_REACHABILITY_SHARE
                verdict = ("resolution-limited" if ambiguous else "measurable")
                if rare:
                    verdict += "; rarely reached"
                cons = r["first_touch_conservative"]
                diag = r["first_touch_diagnostic"]
                print(f"   {horizon:>2} {float(r['bracket_points']):>5.1f} "
                      f"{r['anchors']:>8,} {r['resolved']:>8,} "
                      f"{r['favorable']:>7,} {r['adverse']:>7,} "
                      f"{r['same_bar']:>7,} {r['neither']:>7,} "
                      f"{r['ambiguity_share_of_resolved']:>7.1%} "
                      f"{'' if cons is None else format(cons, '>8.1%')} "
                      f"{'' if diag is None else format(diag, '>8.1%')}"
                      f"  {verdict}")
            print()

    print("5. WHAT IS MEASURABLE WITH ONE-MINUTE OHLCV\n")
    measurable = [r for r in rows
                  if r["ambiguity_share_of_resolved"] <= MAX_AMBIGUITY_SHARE]
    limited = [r for r in rows
               if r["ambiguity_share_of_resolved"] > MAX_AMBIGUITY_SHARE]
    print(f"   declared threshold: a combination is resolution-limited when")
    print(f"   more than {MAX_AMBIGUITY_SHARE:.0%} of its RESOLVED cases are "
          f"same-bar.\n")
    for key in ("A", "B"):
        ok = sorted({(r["horizon_minutes"], r["bracket_points"])
                     for r in measurable if r["window"] == key})
        bad = sorted({(r["horizon_minutes"], r["bracket_points"])
                      for r in limited if r["window"] == key})
        print(f"   window {key}  measurable      : "
              f"{', '.join(f'{h}m/{b}p' for h, b in ok) or 'none'}")
        print(f"   window {key}  resolution-limited: "
              f"{', '.join(f'{h}m/{b}p' for h, b in bad) or 'none'}\n")
    print("   Reachability is reported separately and is NOT a data problem:")
    print("   a bracket that is rarely touched would be rarely touched in")
    print("   tick data too. It bounds how often a trade completes, not")
    print("   whether this data can see it.")
    print("\n   The first-touch columns are the unconditional NULL. They are")
    print("   the bar any conditional edge must clear, not a result.")

    report = {
        "diagnostic": "scalp_resolution_reachability",
        "partition_used": "discovery", "validation_read": False,
        "holdout_read": False, "symbol": args.symbol, "sessions": len(days),
        "opening_window_minutes": OPENING_WINDOW_MINUTES,
        "sample_stride": SAMPLE_STRIDE,
        "max_ambiguity_share": MAX_AMBIGUITY_SHARE,
        "low_reachability_share": LOW_REACHABILITY_SHARE,
        "bar_range_percentiles": {k: percentiles(v) for k, v in ranges.items()},
        "bar_range_mean": {k: statistics.mean(v) for k, v in ranges.items()},
        "bar_count": {k: len(v) for k, v in ranges.items()},
        "rows": rows, "manifest_sha256": manifest["sha256"],
    }
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True,
                                         default=str))
    print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
