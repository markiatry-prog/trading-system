#!/usr/bin/env python3
"""T-004B: the frozen hypotheses, measured against a MATCHED BASELINE.

WHAT THIS IS, AND WHAT IT IS NOT

T-004 asked: is the post-event return different from ZERO? Over
2021-2026 NQ trended upward and its drift is far from uniform through
the day, so that question cannot separate an edge from the clock. T-004B
asks the question that can:

    does this event change the forward distribution relative to a
    comparable NON-EVENT state?

THIS IS DIAGNOSTIC, NOT CONFIRMATORY. The methodology changed AFTER the
original NQ results were seen. That ordering matters more than the
result: a comparison chosen once you know how the first one turned out
is a second look at the same data, and reporting it as though it had
been the plan all along is exactly the substitution this framework
exists to prevent. So every verdict here is labelled EXPLORATORY, and
nothing in this file may be cited as confirmatory evidence.

WHAT IT DOES NOT TOUCH

  - The frozen H1-H12 definitions. Unchanged, and this script cannot
    alter them.
  - The T-004 report. A separate output path; `--out` refuses to write
    over a T-004 report.
  - The final holdout. Not selected, not measured, not checkpointed.
    There is no --unseal-holdout flag here at all: a diagnostic
    re-analysis is the last thing that should be allowed to open it.

WHAT A RESULT MEANS

  lift          event mean minus the standardised control mean, in
                points, signed so positive means the PREREGISTERED
                direction was right
  lift CI       percentile bootstrap resampling SESSIONS as clusters
  lift p        two-sided, from that same clustered distribution
  ev_sess       sessions the EVENT arm occupies -- how many independent
                chances the claim had to be wrong
  eff           effective cluster count. Far below ev_sess means a few
                of those sessions carry the result
  absolute      the T-004 quantity, reported alongside so the two can
                be compared directly

THE INFERENCE UNIT IS THE SESSION. Events inside one session share a
volatility regime, a news cycle, and frequently overlapping forward
windows: two breaks twenty minutes apart are largely the same tape.
Resampling them independently would let one unusual week present itself
as hundreds of confirmations. The event-level interval is reported
alongside, purely so the size of that difference is visible.

A hypothesis whose absolute effect is large and whose lift is ~0 was
measuring the market's drift, not the event.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.features.config import FeatureConfig
from trading_system.features.contracts import ContractTimeline
from trading_system.features.engine import ENGINE_VERSION, FeatureEngine
from trading_system.features.records import FeatureType
from trading_system.market_data import Instrument
from trading_system.research.baseline import (
    BASELINE_VERSION, MatchingSpec, StageArms, compare_conditioning,
    compare_to_baseline, measure_anchor_paths, observations_from_paths)
from trading_system.research.checkpoint import (
    CheckpointError, CheckpointKey, StudyCheckpoint)
from trading_system.research.eligibility import filter_eligible
from trading_system.research.identity import ResearchIdentity
from trading_system.research.outcomes import BarWindowIndex
from trading_system.research.partitions import Partition, split_chronologically
from trading_system.research.preregistered import build_registry
from trading_system.research.progress import StageProgress, format_duration
from trading_system.research.quality import (QualityReport, QualityThresholds,
                                             assess_day)
from trading_system.research.study import (FeatureStateIndex, Study,
                                           matches, required_condition_types)
from trading_system.sources.databento_source import DatabentoFileSource

ANALYSIS_VERSION = "T-004B"
STATUS = "diagnostic/exploratory -- methodology revised after seeing T-004"

INSTRUMENTS = {
    "NQ.c.0": Instrument(symbol="NQ.c.0", product="NQ", tick_size=Decimal("0.25")),
    "MNQ.c.0": Instrument(symbol="MNQ.c.0", product="MNQ", tick_size=Decimal("0.25")),
    "ES.c.0": Instrument(symbol="ES.c.0", product="ES", tick_size=Decimal("0.25")),
}


def verify_manifest(data_dir: Path) -> dict:
    mpath = data_dir / "manifest.json"
    if not mpath.exists():
        raise SystemExit(f"no manifest.json in {data_dir}; provenance is "
                         f"mandatory and this data cannot be used")
    manifest = json.loads(mpath.read_text())
    dbn = data_dir / manifest["file"]
    if not dbn.exists():
        raise SystemExit(f"manifest names {manifest['file']}, which is missing")
    actual = hashlib.sha256(dbn.read_bytes()).hexdigest()
    if actual != manifest["sha256"]:
        raise SystemExit(
            f"INTEGRITY FAILURE: {manifest['file']} hashes to {actual[:16]}..., "
            f"manifest says {manifest['sha256'][:16]}....")
    return manifest


def refuse_to_overwrite_t004(out_path: Path) -> None:
    """The T-004 report is an immutable historical artifact."""
    if not out_path.exists():
        return
    try:
        existing = json.loads(out_path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    prior = (existing.get("research_identity") or {}).get("analysis_version")
    if prior and prior != ANALYSIS_VERSION:
        raise SystemExit(
            f"\n{out_path} holds a {prior} report. T-004B must not overwrite "
            f"it: that report is the record of what was actually "
            f"preregistered and measured. Choose a different --out.\n")


def atomic_write(path: Path, payload: str) -> None:
    tmp = path.with_suffix(path.suffix + ".partial")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_or_start(path: Path, key: CheckpointKey, restart: bool):
    if restart and path.exists():
        path.unlink()
        print(f"   --restart: discarded {path}")
    if not path.exists():
        return StudyCheckpoint(key=key)
    try:
        return StudyCheckpoint.load(path, key)
    except CheckpointError as exc:
        raise SystemExit(
            f"\nCHECKPOINT REFUSED\n  {path}\n  {exc}\n\n"
            f"  Rerun with --restart to discard it.\n") from None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data/t004")
    ap.add_argument("--symbol", default="NQ.c.0", choices=sorted(INSTRUMENTS))
    ap.add_argument("--out", default="t004b_nq.json")
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--restart", action="store_true")
    args = ap.parse_args()

    out_path = Path(args.out)
    refuse_to_overwrite_t004(out_path)

    print(f"{ANALYSIS_VERSION}  ({STATUS})")
    print(f"baseline v{BASELINE_VERSION}\n")

    data_dir = Path(args.data_dir)
    manifest = verify_manifest(data_dir)
    print("1. manifest verified")
    print(f"   {manifest['file']}  {manifest['bytes']:,} bytes  "
          f"sha256 {manifest['sha256'][:32]}...\n")

    instrument = INSTRUMENTS[args.symbol]
    captured_at = datetime.fromisoformat(manifest["finished_at"])
    source = DatabentoFileSource(data_dir / manifest["file"], INSTRUMENTS,
                                 captured_at)
    print("2. resolving symbology and splitting by instrument ...")
    bars = source.bars_by_symbol()[instrument.symbol]
    if not bars:
        raise SystemExit("no bars for this symbol")
    print(f"   {args.symbol}: {len(bars):,} bars\n")

    config = FeatureConfig()
    calendar = FeatureEngine(instrument, config).calendar
    by_day = {}
    for b in bars:
        by_day.setdefault(calendar.session_date_for(b.observed_at), []).append(b)

    print("3. data-quality gate (unchanged from T-004)")
    quality = QualityReport()
    for day, day_bars in sorted(by_day.items()):
        quality.add(assess_day(day, instrument.symbol, day_bars, calendar))
    usable = quality.passed_days
    q = quality.summary()
    print(f"   {q['days_passed']}/{q['days_assessed']} sessions passed\n")
    if len(usable) < 30:
        raise SystemExit(f"only {len(usable)} usable days; refusing to compute")

    contracts = ContractTimeline.from_bars(
        [b for day in usable for b in by_day[day]], calendar)
    parts = split_chronologically(usable)
    registry = build_registry()
    spec = MatchingSpec()

    print("4. partitions   discovery + validation only; the holdout is not")
    print(f"   discovery  {parts.discovery[0]} .. {parts.discovery[1]}")
    print(f"   validation {parts.validation[0]} .. {parts.validation[1]}")
    print(f"   holdout    SEALED and not read by this analysis\n")

    identity = ResearchIdentity.build(
        study_version="1.0.0", engine_version=ENGINE_VERSION,
        symbol=args.symbol, feature_config=config, registry=registry,
        thresholds=QualityThresholds(), passed_days=usable, partitions=parts,
        dataset_sha256=manifest["sha256"], dataset_bytes=int(manifest["bytes"]),
        request_digest=str(manifest.get("request_digest", "")),
        analysis_version=ANALYSIS_VERSION, baseline_digest=spec.digest())
    key = CheckpointKey.for_identity(identity)
    ckpt_path = Path(args.checkpoint or f"{args.out}.checkpoint.json")
    checkpoint = load_or_start(ckpt_path, key, args.restart)

    print(f"5. research identity  {identity.digest()}")
    print(f"   matching spec      {spec.digest()[:32]}")
    print(f"   checkpoint         {ckpt_path}")
    if checkpoint.completed():
        print(f"   RESUMING; complete: {', '.join(checkpoint.completed())}")
    print()

    study = Study(f"t004b-{args.symbol}", parts, registry, quality,
                  contracts=contracts, identity=identity)
    required = required_condition_types(registry)
    all_rows = {}
    run_started = time.perf_counter()

    for stage in (Partition.DISCOVERY, Partition.VALIDATION):
        if checkpoint.has(stage):
            all_rows[stage.value] = checkpoint.rows_for(stage)
            done = checkpoint.stages[stage.value]
            print(f"6. {stage.value:11} RESTORED from checkpoint "
                  f"({len(all_rows[stage.value])} comparisons, "
                  f"{done['completed_at'][11:19]})")
            continue

        days = parts.select(usable, stage)
        engine = FeatureEngine(instrument, config)
        arms = StageArms(calendar, spec)
        conditions = FeatureStateIndex(required)
        events, stage_bars, features_for_conditioning = [], [], []
        progress = StageProgress(f"{stage.value}/scan", len(days))
        for day in days:
            records = engine.run(by_day[day])
            day_events = [r for r in records if r.kind.value == "event"]
            day_features = [r for r in records if r.kind.value != "event"]
            events.extend(day_events)
            for r in day_features:
                conditions.add(r)
            features_for_conditioning.extend(
                r for r in day_features if r.type in required)
            # ATR knowable at each bar close, for the volatility band and
            # the +X-before--Y threshold. available_at == the bar close.
            atr_at = {r.available_at: r.value for r in day_features
                      if r.type == FeatureType.ATR.value and r.value is not None}
            arms.observe_session(day, by_day[day], day_features, atr_at)
            stage_bars.extend(by_day[day])
            progress.advance(len(by_day[day]), len(day_events))
        progress.finish()

        index = BarWindowIndex(stage_bars)

        # ONE control pool for the stage, and its forward paths measured
        # ONCE PER DISTINCT HORIZON rather than once per hypothesis. A
        # path depends on the anchor, the bars and the horizon -- not on
        # which hypothesis is asking or which way it faces -- and the
        # twelve frozen hypotheses use three horizons between them.
        # Per-hypothesis exclusion and direction are applied afterwards,
        # to the measured paths, which is exact and far cheaper.
        pool = arms.build_control_pool(set())
        horizons = sorted({h.horizon_minutes for h in registry.all()})
        prep = StageProgress(f"{stage.value}/controls", len(horizons),
                             unit="horizons", min_interval=0.0,
                             counter_labels=("anchors", "paths"))
        control_paths = {}
        for horizon in horizons:
            control_paths[horizon] = measure_anchor_paths(
                pool.anchors(), stage_bars, horizon, index)
            prep.advance(len(pool.anchors()), len(control_paths[horizon]))
        prep.finish()

        rows = []
        compare = StageProgress(f"{stage.value}/compare", registry.count(),
                                unit="hypotheses", min_interval=0.0,
                                counter_labels=("controls", "events"))
        for h in registry.all():
            candidates = [e for e in events if e.type == h.event_type]
            candidates, eligibility = filter_eligible(h, candidates, contracts)
            # One pass, and no set of FeatureRecords: they carry a dict
            # of attributes, so they are not hashable and membership
            # testing would raise rather than merely be slow.
            conditioned, complement = [], []
            for e in candidates:
                if not h.conditions or matches((), h.conditions,
                                               e.available_at, conditions):
                    conditioned.append(e)
                else:
                    complement.append(e)

            event_obs, unmatched = arms.event_observations(
                conditioned, stage_bars, h.direction, h.horizon_minutes, index)
            control_obs = observations_from_paths(
                control_paths[h.horizon_minutes], h.direction, spec,
                exclude={e.available_at for e in candidates})
            comparison = compare_to_baseline(h, stage.value, event_obs,
                                             control_obs, spec)
            row = comparison.as_row()
            row["eligibility"] = eligibility.as_row()
            row["events_without_a_stratum"] = unmatched
            row["control_pool"] = pool.summary()

            if h.conditions:
                other_obs, _ = arms.event_observations(
                    complement, stage_bars, h.direction, h.horizon_minutes,
                    index)
                row["conditioning_contrast"] = compare_conditioning(
                    h, stage.value, event_obs, other_obs, spec).as_row()
            rows.append(row)
            compare.advance(len(control_obs), len(event_obs))
        compare.finish()

        all_rows[stage.value] = rows
        checkpoint.record_stage(stage, [], study.ledger, len(days),
                                len(events), rows=rows)
        checkpoint.save(ckpt_path)
        print(f"   {stage.value:11} {len(days)} sessions, {len(events):,} "
              f"events; checkpoint saved {checkpoint.updated_at[11:19]}")

    print(f"\n   total {format_duration(time.perf_counter() - run_started)}\n")

    print("7. BASELINE-RELATIVE LIFT (discovery)")
    print("   Inference resamples SESSIONS as clusters. Events inside one")
    print("   session share a regime and overlapping forward windows, so")
    print("   they are not independent observations.\n")
    header = (f"   {'id':4} {'n_ev':>7} {'ev_sess':>7} {'eff':>6} {'n_ctl':>6} "
              f"{'absolute':>9} {'baseline':>9} {'lift':>8} "
              f"{'95% CI (clustered)':>22} {'p':>6}")
    print(header)
    print("   " + "-" * (len(header) - 3))
    for row in all_rows.get(Partition.DISCOVERY.value, []):
        ev, ct = row["event"], row["control"]
        eff = row.get("effective_clusters")
        eff_s = f"{eff:>6.1f}" if eff is not None else "     -"
        if row["lift"] is None or row["lift_ci_low"] is None:
            print(f"   {row['hypothesis_id']:4} {ev['n']:>7} "
                  f"{row.get('event_sessions', 0):>7} {eff_s} {ct['n']:>6}"
                  f"   {row['note'][:56]}")
            continue
        ci = f"[{row['lift_ci_low']:+.2f}, {row['lift_ci_high']:+.2f}]"
        print(f"   {row['hypothesis_id']:4} {ev['n']:>7} "
              f"{row['event_sessions']:>7} {eff_s} {ct['n']:>6} "
              f"{ev['mean_signed_return']:>+9.2f} "
              f"{row['control_standardised_mean']:>+9.2f} "
              f"{row['lift']:>+8.2f} {ci:>22} {row['lift_p_value']:>6.3f}")

    print("\n7b. HOW MUCH THE INFERENCE UNIT MATTERS\n")
    print(f"   {'id':4} {'clustered CI':>24} {'event-level CI':>24} "
          f"{'width':>7}  conclusion")
    print("   " + "-" * 74)
    for row in all_rows.get(Partition.DISCOVERY.value, []):
        if row["lift_ci_low"] is None or row["event_level_ci_low"] is None:
            continue
        clustered = row["lift_ci_high"] - row["lift_ci_low"]
        naive = row["event_level_ci_high"] - row["event_level_ci_low"]
        ratio = clustered / naive if naive else float("inf")
        flips = row["clustering_changes_the_conclusion"]
        verdict = ("CHANGES the conclusion" if flips
                   else "same conclusion")
        print(f"   {row['hypothesis_id']:4} "
              f"[{row['lift_ci_low']:+8.2f},{row['lift_ci_high']:+8.2f}] "
              f"[{row['event_level_ci_low']:+8.2f},"
              f"{row['event_level_ci_high']:+8.2f}] "
              f"{ratio:>6.1f}x  {verdict}")
    print("\n   A large widening factor means the events were concentrated")
    print("   in few sessions and the event-level interval was fiction.")
    print("   Compare n_ev against eff: 400 events over 4 effective")
    print("   clusters are not 400 confirmations.")

    print("\n8. WHAT MOVED WHEN THE BASELINE WAS SUBTRACTED\n")
    for row in all_rows.get(Partition.DISCOVERY.value, []):
        if row["lift"] is None:
            continue
        absolute = row["event"]["mean_signed_return"]
        crossed = row["lift_ci_excludes_zero"]
        if absolute is not None and abs(absolute) > 1 and not crossed:
            print(f"   {row['hypothesis_id']}: absolute {absolute:+.2f} but "
                  f"lift {row['lift']:+.2f} with an interval spanning zero "
                  f"-- this was measuring the market, not the event")

    print("\n9. CONDITIONING CONTRAST (the second clause of the falsifier)\n")
    any_conditioned = False
    for row in all_rows.get(Partition.DISCOVERY.value, []):
        contrast = row.get("conditioning_contrast")
        if not contrast:
            continue
        any_conditioned = True
        if contrast["lift"] is None:
            print(f"   {row['hypothesis_id']}: {contrast['note'][:100]}")
        else:
            verdict = ("the condition ADDS something"
                       if contrast["lift_ci_excludes_zero"]
                       else "the condition adds NOTHING over the "
                            "unconditional case")
            print(f"   {row['hypothesis_id']}: lift {contrast['lift']:+.2f} "
                  f"[{contrast['lift_ci_low']:+.2f}, "
                  f"{contrast['lift_ci_high']:+.2f}] -- {verdict}")
    if not any_conditioned:
        print("   no conditioned hypothesis in the frozen set")

    report = {
        "analysis_version": ANALYSIS_VERSION,
        "status": STATUS,
        "confirmatory": False,
        "preregistered": False,
        "supersedes_t004": False,
        "note": (
            "T-004B re-measures the FROZEN T-004 hypotheses against matched "
            "controls. The methodology was revised after the T-004 results "
            "were seen, so these are diagnostic findings that may generate "
            "candidates for a future preregistered cycle. They are not "
            "confirmatory evidence and do not replace the T-004 report."),
        "manifest": manifest,
        "research_identity": identity.as_row(),
        "matching_spec_digest": spec.digest(),
        "baseline_version": BASELINE_VERSION,
        "holdout_read": False,
        "provenance": study.provenance(),
        "comparisons": all_rows,
    }
    atomic_write(out_path, json.dumps(report, indent=2, sort_keys=True,
                                      default=str))
    print(f"\nwritten to {out_path}")
    if ckpt_path.exists():
        ckpt_path.unlink()
        print(f"checkpoint {ckpt_path} removed; the run completed")
    print(f"\n{ANALYSIS_VERSION} is {STATUS}.")
    print("Nothing here is confirmatory. An interesting lift is a candidate")
    print("to preregister and test on data it has not seen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
