#!/usr/bin/env python3
"""Run the frozen T-004 study against acquired data. ONE command.

Order is fixed and cannot be shuffled:
  1. verify the acquisition manifest       (integrity + provenance)
  2. normalise DBN -> canonical bars       (the vendor stops here)
  3. DATA-QUALITY GATE                     (before any statistic)
  3b. contract provenance per session      (eligibility, not quality)
  4. chronological partitions              (holdout SEALED)
  5. load the frozen preregistered set     (already sealed in code)
  6. run discovery, then validation        (holdout untouched)
  7. classify with multiplicity correction
  8. report, with sample sizes and intervals

The final holdout is NOT opened by this script. Opening it is a separate,
deliberate act after the hypothesis set is final -- `--unseal-holdout`
exists, demands a reason, and records it permanently.

RESUMABLE. Each non-holdout stage is checkpointed as it completes, so a
reboot costs the current stage rather than the whole run -- which it
twice did. The checkpoint carries a digest of everything that
determines the output (dataset, symbol, engine, config, hypothesis
structure, quality verdict, partitions, and the source of every module
that computes a number) and refuses to resume if any of it moved. The
holdout is never written to one. See research/checkpoint.py.

WHY IT USED TO TAKE DAYS. Two quadratic terms, both invisible:

  measure_forward_path rebuilt the list of bars after an event by
  scanning the whole stage, once per event -- O(events x bars).

  condition_state scanned every feature record to answer each
  conditioning question -- O(events x features).

Measured on synthetic sessions of the real shape, the twelve
hypotheses cost 64 s over ten sessions and 251 s over twenty: a
four-fold rise for a doubling, extrapolating to about eleven days at
1,234. Both are now located by bisection over a sorted index, and the
engine no longer rescans FVGs that can never fire again. The numbers
are unchanged and tests/test_equivalence.py is the evidence.
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
from trading_system.market_data import Instrument
from trading_system.research.checkpoint import (
    CHECKPOINT_VERSION, CheckpointError, CheckpointKey, StudyCheckpoint,
    code_digest, partition_digest, quality_digest,
    registry_structure_digest)
from trading_system.research.classify import Verdict
from trading_system.research.outcomes import BarWindowIndex
from trading_system.research.partitions import Partition, split_chronologically
from trading_system.research.preregistered import build_registry
from trading_system.research.progress import StageProgress, format_duration
from trading_system.research.quality import (QualityReport, QualityThresholds,
                                             assess_day)
from trading_system.research.study import (FeatureStateIndex, Study,
                                           required_condition_types)
from trading_system.sources.databento_source import DatabentoFileSource

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
            f"manifest says {manifest['sha256'][:16]}.... The file has changed "
            f"since acquisition; refusing to compute on it.")
    return manifest


def load_or_start(path: Path, key: CheckpointKey,
                  restart: bool) -> StudyCheckpoint:
    """Resume, or refuse and say why. Never silently discard.

    A checkpoint written under different inputs is not merely useless --
    resuming from it would splice results computed under one version of
    the code or the data into a report describing another. So a mismatch
    stops the run and names what changed; discarding it has to be the
    operator's explicit choice.
    """
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
            f"  Rerun with --restart to discard it and compute from the "
            f"beginning.\n") from None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/t004")
    ap.add_argument("--symbol", default="NQ.c.0", choices=sorted(INSTRUMENTS))
    ap.add_argument("--unseal-holdout", metavar="REASON", default=None,
                    help="open the final holdout. Requires a substantive "
                         "reason, recorded permanently. Do NOT use while "
                         "developing or selecting hypotheses.")
    ap.add_argument("--out", default=None, help="write the full report as JSON")
    ap.add_argument("--checkpoint", default=None,
                    help="checkpoint file (default: <out>.checkpoint.json, "
                         "or t004-<symbol>.checkpoint.json). Completed "
                         "stages are reloaded instead of recomputed.")
    ap.add_argument("--restart", action="store_true",
                    help="discard an existing checkpoint and start over. "
                         "Required to proceed when a checkpoint exists but "
                         "was written under different inputs.")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    manifest = verify_manifest(data_dir)
    print("1. manifest verified")
    print(f"   file {manifest['file']}  {manifest['bytes']:,} bytes")
    print(f"   sha256 {manifest['sha256'][:32]}...")
    print(f"   acquired {manifest['finished_at']}  cost "
          f"${manifest['repriced_cost_usd']:.2f}\n")

    instrument = INSTRUMENTS[args.symbol]
    captured_at = datetime.fromisoformat(manifest["finished_at"])
    # ALL symbols are handed to the source, because one DBN file holds
    # every symbol that was requested together and they are separated
    # only by instrument_id. Passing a single instrument would pool three
    # contracts at three price levels into one series.
    source = DatabentoFileSource(data_dir / manifest["file"], INSTRUMENTS,
                                 captured_at)

    print("2. resolving symbology and splitting by instrument ...")
    by_symbol = source.bars_by_symbol()
    total_kept = 0
    for sym in sorted(by_symbol):
        series = by_symbol[sym]
        total_kept += len(series)
        if not series:
            print(f"   {sym:9} {0:>10,} bars")
            continue
        span = (series[-1].observed_at - series[0].observed_at).days or 1
        per_session = len(series) / max(1, span * 5 / 7)
        # A session cannot contain more bars than it has minutes. If it
        # does, instruments have been pooled and every level, VWAP and
        # excursion computed from the series would be meaningless.
        flag = "  POOLED" if per_session > 1440 else ""
        print(f"   {sym:9} {len(series):>10,} bars  "
              f"{per_session:>7,.0f}/session{flag}")
        if per_session > 1440:
            raise SystemExit(
                f"{sym}: {per_session:,.0f} bars per trading day exceeds the "
                f"1,440 minutes a day contains. Instruments are pooled; "
                f"refusing to compute.")

    # Nothing lost, nothing duplicated.
    read = getattr(source, "last_record_total", None)
    if read is not None:
        print(f"   reconciliation: {total_kept:,} kept of {read:,} read")
        if total_kept != read:
            raise SystemExit(
                f"{read - total_kept:,} records were neither kept nor "
                f"refused; the sample would silently differ from the file.")

    bars = by_symbol[instrument.symbol]
    print(f"   using {instrument.symbol}\n")
    if not bars:
        raise SystemExit("no bars for this symbol; check the acquisition")

    config = FeatureConfig()
    calendar = FeatureEngine(instrument, config).calendar
    by_day = {}
    for b in bars:
        by_day.setdefault(calendar.session_date_for(b.observed_at), []).append(b)

    print("3. data-quality gate (before any statistic)")
    quality = QualityReport()
    for day, day_bars in sorted(by_day.items()):
        quality.add(assess_day(day, instrument.symbol, day_bars, calendar))
    q = quality.summary()
    print(f"   {q['days_passed']}/{q['days_assessed']} days passed, "
          f"{q['days_excluded']} excluded")
    for reason, n in sorted(q["exclusion_reasons"].items(),
                            key=lambda kv: -kv[1]):
        print(f"     {n:>5}  EXCLUDED  {reason}")
    for obs, n in sorted(q["observations"].items(), key=lambda kv: -kv[1]):
        print(f"     {n:>5}  observed  {obs}")
    usable = quality.passed_days
    if len(usable) < 30:
        raise SystemExit(f"only {len(usable)} usable days; refusing to compute")

    # Built from the days that PASSED the gate, in the order the engine
    # will see them: "prior session" must mean the session whose level
    # actually gets carried forward, not the previous calendar day.
    contracts = ContractTimeline.from_bars(
        [b for day in usable for b in by_day[day]], calendar)
    cp = contracts.summary()
    print(f"\n3b. contract provenance (from symbology, not the calendar)")
    print(f"   {cp['distinct_contracts']} distinct contracts across "
          f"{cp['sessions']} sessions")
    for verdict, n in sorted(cp["verdicts"].items(), key=lambda kv: -kv[1]):
        print(f"     {n:>5}  {verdict}")
    if cp["transitions"]:
        print(f"   contract boundaries:")
        for t in cp["transitions"]:
            print(f"     {t['session_date']}  "
                  f"{t['prior_contract_id']} -> {t['contract_id']}")

    parts = split_chronologically(usable)
    print(f"\n4. partitions")
    print(f"   discovery  {parts.discovery[0]} .. {parts.discovery[1]}")
    print(f"   validation {parts.validation[0]} .. {parts.validation[1]}")
    print(f"   holdout    {parts.holdout[0]} .. {parts.holdout[1]}  SEALED")

    registry = build_registry()
    print(f"\n5. frozen hypothesis set: {registry.count()} hypotheses, "
          f"chain valid={registry.verify()}, sealed={registry.sealed}")

    study = Study(f"t004-{args.symbol}", parts, registry, quality,
                  contracts=contracts)

    stages = [Partition.DISCOVERY, Partition.VALIDATION]
    if args.unseal_holdout:
        parts.unseal(args.unseal_holdout)
        stages.append(Partition.HOLDOUT)
        print(f"\n   HOLDOUT UNSEALED: {args.unseal_holdout}")

    # -- checkpoint --------------------------------------------------
    # The key digests everything that determines the output. Resuming
    # under anything else is refused, not reconciled.
    key = CheckpointKey(
        checkpoint_version=CHECKPOINT_VERSION,
        study_version=study.provenance()["study_version"],
        engine_version=ENGINE_VERSION,
        symbol=args.symbol,
        dataset_sha256=manifest["sha256"],
        dataset_bytes=int(manifest["bytes"]),
        request_digest=str(manifest.get("request_digest", "")),
        config_digest=config.digest(),
        config_name=config.name,
        registry_structure=registry_structure_digest(registry),
        quality=quality_digest(QualityThresholds(), usable),
        partitions=partition_digest(parts),
        code=code_digest(),
    )
    ckpt_path = Path(args.checkpoint or (
        (args.out + ".checkpoint.json") if args.out
        else f"t004-{args.symbol}.checkpoint.json"))
    checkpoint = load_or_start(ckpt_path, key, args.restart)

    print(f"\n5b. checkpoint {ckpt_path}")
    print(f"    key {key.digest()[:32]}")
    if checkpoint.completed():
        print(f"    RESUMING -- already complete: "
              f"{', '.join(checkpoint.completed())}")
    else:
        print("    no reusable stage; starting from the first")

    print()
    required_features = required_condition_types(registry)
    print(f"6. stages  (conditioning reads {sorted(required_features) or 'no'} "
          f"feature types; the rest are not retained)")
    run_started = time.perf_counter()
    for stage in stages:
        days = parts.select(usable, stage)
        if checkpoint.has(stage):
            n = checkpoint.restore_into(study, stage)
            done = checkpoint.stages[stage.value]
            print(f"   {stage.value:11} {len(days):>4} days  "
                  f"{done['events']:>8,} events  RESTORED from checkpoint "
                  f"({n} results, completed {done['completed_at'][11:19]})")
            continue

        # ONE engine, fed the whole stage in order. A fresh engine per
        # day never carries a prior-day level forward, so
        # _detect_sweeps could not fire on any reference and H6/H7 had
        # exactly zero events -- a silent floor, not an error. The
        # engine is designed to consume a continuous stream; this feeds
        # it one.
        #
        # A separate engine PER STAGE, not one across all of them,
        # because a single engine would have to be fed the holdout's
        # bars to reach the holdout's records. The cost is that the
        # first day of each stage has no prior session, which the
        # contract rule already reports as no_prior_session.
        engine = FeatureEngine(instrument, config)
        # Features are indexed as they stream out rather than collected.
        # The engine emits ~17 per bar and conditioning reads one of
        # them, so keeping the list would hold ~30M records to consult
        # ~2M -- which is what exhausted the machine.
        conditions = FeatureStateIndex(required_features)
        events, stage_bars = [], []
        progress = StageProgress(stage.value, len(days))
        if checkpoint.completed():
            # Only a checkpoint that actually holds a stage; a freshly
            # constructed one has a timestamp but nothing behind it.
            progress.note_checkpoint(checkpoint.updated_at)
        for day in days:
            before = len(events)
            for record in engine.run(by_day[day]):
                if record.kind.value == "event":
                    events.append(record)
                else:
                    conditions.add(record)
            stage_bars.extend(by_day[day])
            progress.advance(len(by_day[day]), len(events) - before)
        bar_index = BarWindowIndex(stage_bars)
        for h in registry.all():
            study.test(h.id, stage, events, (), stage_bars,
                       bar_index=bar_index, condition_index=conditions)
        elapsed = progress.finish()

        if stage is Partition.HOLDOUT:
            print(f"   {stage.value:11} not checkpointed by design")
        else:
            stage_results = [r for r in study.results
                             if r.partition == stage.value]
            checkpoint.record_stage(stage, stage_results, study.ledger,
                                    len(days), len(events))
            checkpoint.save(ckpt_path)
            progress.note_checkpoint(checkpoint.updated_at)
            print(f"   {stage.value:11} done in {format_duration(elapsed)}; "
                  f"checkpoint saved {checkpoint.updated_at[11:19]}")
    print(f"   total stage time {format_duration(time.perf_counter() - run_started)}")

    print("\n7. verdicts (Benjamini-Hochberg corrected across discovery)\n")
    verdicts = study.classify_all()
    print(f"   {'id':4} {'verdict':13} {'n':>6} {'mean':>9} "
          f"{'95% CI':>20} {'MFE':>7} {'MAE':>7} {'fav1st':>7}")
    print("   " + "-" * 82)
    for hid in sorted(verdicts, key=lambda k: int(k[1:])):
        c = verdicts[hid]
        r = next((x for x in study.results
                  if x.hypothesis_id == hid and x.partition == "discovery"), None)
        if r and r.estimate:
            e = r.estimate
            ci = f"[{e.ci_low:+.3f}, {e.ci_high:+.3f}]"
            mfe = f"{r.mfe_mean:.2f}" if r.mfe_mean is not None else "-"
            mae = f"{r.mae_mean:.2f}" if r.mae_mean is not None else "-"
            fav = (f"{r.favorable_first_fraction:.0%}"
                   if r.favorable_first_fraction is not None else "-")
            print(f"   {hid:4} {c.verdict.value:13} {e.n:>6} {e.mean:>+9.3f} "
                  f"{ci:>20} {mfe:>7} {mae:>7} {fav:>7}")
        else:
            print(f"   {hid:4} {c.verdict.value:13} {'0':>6}  (no events)")

    print("\n8. reasons")
    for hid in sorted(verdicts, key=lambda k: int(k[1:])):
        print(f"   {hid}: {verdicts[hid].reason}")

    counts = {}
    for c in verdicts.values():
        counts[c.verdict.value] = counts.get(c.verdict.value, 0) + 1
    print(f"\n9. summary {counts}")
    print(f"   tests actually run (snooping ledger): {study.ledger.total_tests}")
    print(f"   holdout ever unsealed: "
          f"{study.provenance()['partitions']['holdout_ever_unsealed']}")
    print(f"   reproducibility digest: {study.reproducibility_digest()[:32]}")

    print("\n10. PREDICTIVE vs TRADABLE")
    for hid in sorted(verdicts, key=lambda k: int(k[1:])):
        c = verdicts[hid]
        if c.verdict in (Verdict.PROMISING, Verdict.ROBUST):
            t = ("capturable" if c.tradable else
                 "NOT capturable -- adverse excursion usually first"
                 if c.tradable is False else "ordering undetermined")
            print(f"   {hid}: {c.verdict.value} / {t}")
    if not any(c.verdict in (Verdict.PROMISING, Verdict.ROBUST)
               for c in verdicts.values()):
        print("   nothing reached PROMISING; no tradability question arises.")

    if args.out:
        report = {"manifest": manifest, "provenance": study.provenance(),
                  "verdicts": {k: v.as_row() for k, v in verdicts.items()}}
        # ATOMIC. A crash or power loss mid-write must leave either the
        # previous file or a complete new one -- never a truncated one
        # that parses far enough to look finished. This exact ambiguity
        # cost a session after an unexpected shutdown.
        out_path = Path(args.out)
        tmp = out_path.with_suffix(out_path.suffix + ".partial")
        payload = json.dumps(report, indent=2, sort_keys=True, default=str)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())      # durable before the rename
        os.replace(tmp, out_path)          # atomic on Windows and POSIX
        print(f"\nfull report written to {args.out} ({len(payload):,} bytes)")

    # The report is complete, so the checkpoint has nothing left to
    # resume. Removing it last means a crash before this point still
    # leaves the work recoverable.
    if ckpt_path.exists():
        ckpt_path.unlink()
        print(f"checkpoint {ckpt_path} removed; the run completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
