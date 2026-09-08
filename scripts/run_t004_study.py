#!/usr/bin/env python3
"""Run the frozen T-004 study against acquired data. ONE command.

Order is fixed and cannot be shuffled:
  1. verify the acquisition manifest       (integrity + provenance)
  2. normalise DBN -> canonical bars       (the vendor stops here)
  3. DATA-QUALITY GATE                     (before any statistic)
  4. chronological partitions              (holdout SEALED)
  5. load the frozen preregistered set     (already sealed in code)
  6. run discovery, then validation        (holdout untouched)
  7. classify with multiplicity correction
  8. report, with sample sizes and intervals

The final holdout is NOT opened by this script. Opening it is a separate,
deliberate act after the hypothesis set is final -- `--unseal-holdout`
exists, demands a reason, and records it permanently.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.features.config import FeatureConfig
from trading_system.features.engine import FeatureEngine
from trading_system.market_data import Instrument
from trading_system.research.classify import Verdict
from trading_system.research.partitions import Partition, split_chronologically
from trading_system.research.preregistered import build_registry
from trading_system.research.quality import QualityReport, assess_day
from trading_system.research.study import Study
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/t004")
    ap.add_argument("--symbol", default="NQ.c.0", choices=sorted(INSTRUMENTS))
    ap.add_argument("--unseal-holdout", metavar="REASON", default=None,
                    help="open the final holdout. Requires a substantive "
                         "reason, recorded permanently. Do NOT use while "
                         "developing or selecting hypotheses.")
    ap.add_argument("--out", default=None, help="write the full report as JSON")
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
    source = DatabentoFileSource(data_dir / manifest["file"], instrument,
                                 captured_at)

    print(f"2. normalising {args.symbol} ...")
    lo = datetime(1970, 1, 1, tzinfo=timezone.utc)
    hi = datetime(2100, 1, 1, tzinfo=timezone.utc)
    from trading_system.market_data import MarketDataSchema
    bars = [b for b in source.history(instrument, MarketDataSchema.BARS, lo, hi)
            if b.instrument.symbol == instrument.symbol]
    print(f"   {len(bars):,} bars\n")
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
        quality.add(assess_day(day, instrument.symbol, day_bars,
                               expected_bars=1380))
    q = quality.summary()
    print(f"   {q['days_passed']}/{q['days_assessed']} days passed, "
          f"{q['days_excluded']} excluded")
    for reason, n in sorted(q["exclusion_reasons"].items()):
        print(f"     {n:>5}  {reason}")
    usable = quality.passed_days
    if len(usable) < 30:
        raise SystemExit(f"only {len(usable)} usable days; refusing to compute")

    parts = split_chronologically(usable)
    print(f"\n4. partitions")
    print(f"   discovery  {parts.discovery[0]} .. {parts.discovery[1]}")
    print(f"   validation {parts.validation[0]} .. {parts.validation[1]}")
    print(f"   holdout    {parts.holdout[0]} .. {parts.holdout[1]}  SEALED")

    registry = build_registry()
    print(f"\n5. frozen hypothesis set: {registry.count()} hypotheses, "
          f"chain valid={registry.verify()}, sealed={registry.sealed}")

    study = Study(f"t004-{args.symbol}", parts, registry, quality)

    stages = [Partition.DISCOVERY, Partition.VALIDATION]
    if args.unseal_holdout:
        parts.unseal(args.unseal_holdout)
        stages.append(Partition.HOLDOUT)
        print(f"\n   HOLDOUT UNSEALED: {args.unseal_holdout}")

    print()
    for stage in stages:
        days = parts.select(usable, stage)
        events, features, stage_bars = [], [], []
        for day in days:
            recs = FeatureEngine(instrument, config).run(by_day[day])
            events.extend(r for r in recs if r.kind.value == "event")
            features.extend(r for r in recs if r.kind.value == "feature")
            stage_bars.extend(by_day[day])
        for h in registry.all():
            study.test(h.id, stage, events, features, stage_bars)
        print(f"6. {stage.value:11} {len(days):>4} days  {len(events):>7} events")

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
        Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True,
                                             default=str))
        print(f"\nfull report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
