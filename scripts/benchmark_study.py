#!/usr/bin/env python3
"""Before/after evidence for the T-004 study's runtime.

Runs the same synthetic workload through both paths and reports the
wall-clock cost of each stage:

  reference   scanning every bar per event, scanning every feature
              record per condition -- the code as it was
  optimized   the same measurements, with the window and the
              conditioning record located by bisection

Both produce identical numbers (see tests/test_equivalence.py); this
script measures only what they cost. Synthetic bars are used so the
benchmark runs anywhere, including CI, with no dataset -- the shape is
the real one (1,397 one-minute bars per session, ~740 events).

The reference path is QUADRATIC in sessions, so `--sizes` deliberately
stays small: the point is to measure the growth rate and extrapolate,
not to sit through it. `--skip-reference` measures the optimized path
alone at realistic sizes.

WHAT THE ENGINE COLUMN DOES AND DOES NOT COMPARE. Both arms run the
current engine, so its two memory fixes -- dropping FVGs that have
inverted, and no longer accumulating two lists nothing reads -- are in
both and do not appear as a difference here. The column compares only
whether the caller RETAINS every feature record or indexes the ones
conditioning can read as they stream. The FVG fix was measured
separately, before and after: 17.08 s to 11.51 s over forty sessions,
with byte-identical output (tests/test_equivalence.py asserts the
records are unchanged).

So the reference total is a fair floor for the old runtime, not a
recreation of it: the old code was slower still.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from trading_system.features.config import FeatureConfig
from trading_system.features.contracts import ContractTimeline
from trading_system.features.engine import FeatureEngine
from trading_system.research.outcomes import BarWindowIndex
from trading_system.research.partitions import Partition, split_chronologically
from trading_system.research.preregistered import build_registry
from trading_system.research.progress import format_duration
from trading_system.research.quality import QualityReport
from trading_system.research.study import (FeatureStateIndex, Study,
                                           required_condition_types)

from fixtures_market import NQ, synthetic_sessions

REAL_SESSIONS = 1234          # NQ sessions that passed the revised gate
REAL_BARS_PER_SESSION = 1397


def engine_pass(days, keep_all_features):
    """Returns (events, features_or_index, seconds)."""
    registry = build_registry()
    engine = FeatureEngine(NQ, FeatureConfig())
    events = []
    features = [] if keep_all_features else None
    index = None if keep_all_features else FeatureStateIndex(
        required_condition_types(registry))
    t0 = time.perf_counter()
    for i in sorted(days):
        for record in engine.run(days[i]):
            if record.kind.value == "event":
                events.append(record)
            elif keep_all_features:
                features.append(record)
            else:
                index.add(record)
    return events, (features if keep_all_features else index), \
        time.perf_counter() - t0


def study_pass(days, bars, events, carried, optimized):
    registry = build_registry()
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(len(days))]
    study = Study("bench", split_chronologically(day_list), registry,
                  QualityReport(),
                  contracts=ContractTimeline.from_bars(
                      bars, FeatureEngine(NQ).calendar),
                  # Without this the reference arm builds an index of its
                  # own and measures the optimized path twice.
                  accelerate=optimized)
    t0 = time.perf_counter()
    if optimized:
        bar_index = BarWindowIndex(bars)
        for h in registry.all():
            study.test(h.id, Partition.DISCOVERY, events, (), bars,
                       bar_index=bar_index, condition_index=carried)
    else:
        for h in registry.all():
            study.test(h.id, Partition.DISCOVERY, events, carried, bars)
    return study, time.perf_counter() - t0


def extrapolate(sizes, times, target, exponent):
    """Scale the largest measurement to the real session count."""
    n, t = sizes[-1], times[-1]
    return t * (target / n) ** exponent


def observed_exponent(sizes, times):
    if len(sizes) < 2 or times[-2] <= 0:
        return 1.0
    import math
    return math.log(times[-1] / times[-2]) / math.log(sizes[-1] / sizes[-2])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", default="5,10,20",
                    help="session counts to measure (default: %(default)s)")
    ap.add_argument("--minutes", type=int, default=REAL_BARS_PER_SESSION,
                    help="bars per session (default: the real %(default)s)")
    ap.add_argument("--skip-reference", action="store_true",
                    help="measure only the optimized path, so larger "
                         "session counts finish in reasonable time")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    sizes = [int(x) for x in args.sizes.split(",")]
    rows = []
    print(f"workload: {args.minutes} one-minute bars per session\n")
    header = (f"{'sessions':>9} {'bars':>10} {'events':>8} "
              f"{'engine':>10} {'study':>10} {'total':>10}  path")
    print(header)
    print("-" * len(header))

    for n in sizes:
        days = synthetic_sessions(n, minutes=args.minutes)
        bars = [b for i in sorted(days) for b in days[i]]
        row = {"sessions": n, "bars": len(bars)}

        events, index, eng_fast = engine_pass(days, keep_all_features=False)
        _study, st_fast = study_pass(days, bars, events, index, optimized=True)
        row.update(events=len(events), engine_optimized=eng_fast,
                   study_optimized=st_fast)
        print(f"{n:>9} {len(bars):>10,} {len(events):>8,} "
              f"{eng_fast:>9.2f}s {st_fast:>9.2f}s "
              f"{eng_fast + st_fast:>9.2f}s  optimized")

        if not args.skip_reference:
            ev2, feats, eng_ref = engine_pass(days, keep_all_features=True)
            _s2, st_ref = study_pass(days, bars, ev2, feats, optimized=False)
            row.update(engine_reference=eng_ref, study_reference=st_ref,
                       features_retained=len(feats))
            print(f"{n:>9} {len(bars):>10,} {len(ev2):>8,} "
                  f"{eng_ref:>9.2f}s {st_ref:>9.2f}s "
                  f"{eng_ref + st_ref:>9.2f}s  reference")
            print(f"{'':>9} {'':>10} {'speedup':>8} "
                  f"{eng_ref / eng_fast:>9.1f}x {st_ref / st_fast:>9.1f}x "
                  f"{(eng_ref + st_ref) / (eng_fast + st_fast):>9.1f}x")
        rows.append(row)

    print(f"\nEXTRAPOLATION to the real NQ sample ({REAL_SESSIONS} sessions)")
    fast_total = [r["engine_optimized"] + r["study_optimized"] for r in rows]
    e_fast = observed_exponent(sizes, fast_total)
    print(f"  optimized  growth n^{e_fast:.2f}  ->  "
          f"{format_duration(extrapolate(sizes, fast_total, REAL_SESSIONS, e_fast))}")
    if not args.skip_reference:
        ref_total = [r["engine_reference"] + r["study_reference"] for r in rows]
        e_ref = observed_exponent(sizes, ref_total)
        print(f"  reference  growth n^{e_ref:.2f}  ->  "
              f"{format_duration(extrapolate(sizes, ref_total, REAL_SESSIONS, e_ref))}")
        print("\n  An exponent near 2 means the cost grows with the SQUARE of")
        print("  the sample: the reason the real run never finished.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"sizes": sizes, "minutes": args.minutes, "rows": rows},
            indent=2, sort_keys=True))
        print(f"\nwritten to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
