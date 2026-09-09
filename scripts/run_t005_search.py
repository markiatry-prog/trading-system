#!/usr/bin/env python3
"""T-005: search the DISCOVERY partition for the next hypothesis.

Hypothesis GENERATION. The output is a candidate that has earned the
right to be preregistered and tested, or the finding that none has. It
is not evidence that anything works, and the winner's numbers are
inflated by the act of selecting it from thousands -- that is
arithmetic, not modesty.

THE ORDER THIS RUNS IN IS THE POINT

  1. enumerate the whole census        before any outcome is read
  2. print the frozen digests          space, criteria, matching
  3. build tagged observations         discovery sessions only
  4. screen every candidate            counts and point lift only
  5. assess the shortlist              stability, then inference
  6. select by formula                 no inspection, no judgement
  7. write the complete search log     every candidate, including the
                                       ones that failed a gate

VALIDATION AND HOLDOUT ARE STRUCTURALLY UNREACHABLE. This script names
Partition.DISCOVERY and nothing else. There is no flag, no argument and
no code path that selects another partition, and a test parses this
file to prove it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.features.config import FeatureConfig
from trading_system.features.contracts import ContractTimeline
from trading_system.features.engine import ENGINE_VERSION, FeatureEngine
from trading_system.features.records import EventType, FeatureType
from trading_system.market_data import Instrument
from trading_system.research.baseline import (
    MatchingSpec, StageArms, measure_anchor_paths)
from trading_system.research.discovery import (
    SEARCH_VERSION, Candidate, SearchSpace, SelectionCriteria, TAG_ORDER,
    Tagged, assess, enumerate_candidates, screen_candidates, select_winner)
from trading_system.research.identity import ResearchIdentity
from trading_system.research.outcomes import BarWindowIndex
from trading_system.research.partitions import Partition, split_chronologically
from trading_system.research.preregistered import build_registry
from trading_system.research.progress import StageProgress, format_duration
from trading_system.research.quality import (QualityReport, QualityThresholds,
                                             assess_day)
from trading_system.sources.databento_source import DatabentoFileSource

ANALYSIS_VERSION = "T-005-SEARCH"
STATUS = "hypothesis generation -- selection-inflated, not evidence"

INSTRUMENTS = {
    "NQ.c.0": Instrument(symbol="NQ.c.0", product="NQ", tick_size=Decimal("0.25")),
    "MNQ.c.0": Instrument(symbol="MNQ.c.0", product="MNQ", tick_size=Decimal("0.25")),
    "ES.c.0": Instrument(symbol="ES.c.0", product="ES", tick_size=Decimal("0.25")),
}

VOLUME_LOW, VOLUME_HIGH = Decimal("0.7"), Decimal("1.3")


def verify_manifest(data_dir: Path) -> dict:
    mpath = data_dir / "manifest.json"
    if not mpath.exists():
        raise SystemExit(f"no manifest.json in {data_dir}")
    manifest = json.loads(mpath.read_text())
    dbn = data_dir / manifest["file"]
    if not dbn.exists():
        raise SystemExit(f"manifest names {manifest['file']}, which is missing")
    actual = hashlib.sha256(dbn.read_bytes()).hexdigest()
    if actual != manifest["sha256"]:
        raise SystemExit(f"INTEGRITY FAILURE on {manifest['file']}")
    return manifest


def session_phase(at, rth_open, rth_close) -> str:
    if not (rth_open <= at < rth_close):
        return "overnight"
    minutes = (at - rth_open).total_seconds() / 60.0
    if minutes < 30:
        return "open30"
    if minutes < 120:
        return "mid"
    return "late"


def band(value, low, high, names) -> str:
    if value is None:
        return names[1]
    return names[0] if value < low else (names[2] if value > high else names[1])


def build_tags(records, bars, calendar, session_date, regime, reference,
               structure_window):
    """Tag every bar close in one session with its market state.

    Built by streaming the engine's own records in order and snapshotting
    at each bar close, so a tag can only ever reflect what was knowable
    then -- the same discipline the study uses for conditioning.
    """
    rth_open = calendar.rth_open_at(session_date)
    rth_close = calendar.rth_close_at(session_date)
    latest = {}
    structure_at = None
    structure_side = None
    tags = {}
    by_time = {}
    for r in records:
        by_time.setdefault(r.available_at, []).append(r)
    for bar in bars:
        at = bar.closed_at
        for r in by_time.get(at, ()):
            if r.kind.value == "event":
                if r.type == EventType.STRUCTURE_BREAK_UP.value:
                    structure_at, structure_side = at, "up"
                elif r.type == EventType.STRUCTURE_BREAK_DOWN.value:
                    structure_at, structure_side = at, "down"
            else:
                latest[r.type] = r
        atr = latest.get(FeatureType.ATR.value)
        atr_value = atr.value if atr is not None else None
        volume = latest.get(FeatureType.VOLUME_RATIO.value)
        vwap = latest.get(FeatureType.ABOVE_VWAP.value)
        to_high = latest.get(FeatureType.DISTANCE_TO_OPENING_RANGE_HIGH.value)
        to_low = latest.get(FeatureType.DISTANCE_TO_OPENING_RANGE_LOW.value)
        if to_high is not None and to_high.value is not None and to_high.value > 0:
            location = "above_or"
        elif to_low is not None and to_low.value is not None and to_low.value < 0:
            location = "below_or"
        else:
            location = "inside_or"
        if structure_at is not None and \
                at - structure_at <= timedelta(minutes=structure_window):
            bias = structure_side
        else:
            bias = "none"
        tags[at] = (
            session_phase(at, rth_open, rth_close),
            regime.band(atr_value, reference),
            band(volume.value if volume is not None else None,
                 VOLUME_LOW, VOLUME_HIGH, ("low", "normal", "high")),
            (vwap.state if vwap is not None and vwap.state else "above"),
            location,
            bias,
        )
    return tags


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data/t004")
    ap.add_argument("--symbol", default="NQ.c.0", choices=sorted(INSTRUMENTS))
    ap.add_argument("--out", default="t005_search.json")
    args = ap.parse_args()

    space, criteria, matching = SearchSpace(), SelectionCriteria(), MatchingSpec()
    census = enumerate_candidates(space)

    print(f"{ANALYSIS_VERSION}  ({STATUS})")
    print(f"search v{SEARCH_VERSION}\n")
    print("0. FROZEN BEFORE ANY OUTCOME IS READ")
    print(f"   search space     {space.digest()}")
    print(f"   criteria         {criteria.digest()}")
    print(f"   matching         {matching.digest()}")
    print(f"   census           {len(census):,} candidates")
    print(f"   CI gate          {criteria.ci_confidence:.0%} clustered "
          f"interval must exclude zero")
    print(f"   family-wise ref  "
          f"{criteria.reported_family_wise_alpha / len(census):.3g}  "
          f"(Bonferroni over the census -- REPORTED, not gated: a")
    print(f"                    percentile bootstrap cannot express a p that "
          f"small, so gating")
    print(f"                    on it would guarantee 'no candidate' whatever "
          f"the data said)")
    print(f"   what controls    stability: the sign must hold across three")
    print(f"   the mining       chronological thirds, three volatility "
          f"regimes, and the")
    print(f"                    dropping of each condition in turn\n")

    data_dir = Path(args.data_dir)
    manifest = verify_manifest(data_dir)
    instrument = INSTRUMENTS[args.symbol]
    captured_at = datetime.fromisoformat(manifest["finished_at"])
    source = DatabentoFileSource(data_dir / manifest["file"], INSTRUMENTS,
                                 captured_at)
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
    print(f"2. quality gate: {len(usable)} sessions passed")

    parts = split_chronologically(usable)
    # THE ONLY PARTITION THIS SCRIPT NAMES.
    days = parts.select(usable, Partition.DISCOVERY)
    print(f"3. discovery ONLY: {len(days)} sessions, "
          f"{days[0]} .. {days[-1]}")
    print(f"   validation and holdout are not selected, read or measured\n")

    contracts = ContractTimeline.from_bars(
        [b for d in usable for b in by_day[d]], calendar)
    engine = FeatureEngine(instrument, config)
    arms = StageArms(calendar, matching)
    thirds = [days[len(days) // 3], days[2 * len(days) // 3]]

    all_tags = {}
    events_by_type = {}
    stage_bars = []
    recent_by_time = {}
    progress = StageProgress("discovery/scan", len(days))
    for day in days:
        records = engine.run(by_day[day])
        day_events = [r for r in records if r.kind.value == "event"]
        day_features = [r for r in records if r.kind.value != "event"]
        reference = arms.regime.reference_for(day)
        all_tags.update(build_tags(records, by_day[day], calendar, day,
                                   arms.regime, reference,
                                   space.structure_bias_window_minutes))
        atr_at = {r.available_at: r.value for r in day_features
                  if r.type == FeatureType.ATR.value and r.value is not None}
        arms.observe_session(day, by_day[day], day_features, atr_at)
        for e in day_events:
            events_by_type.setdefault(e.type, []).append(e)
        stage_bars.extend(by_day[day])
        progress.advance(len(by_day[day]), len(day_events))
    progress.finish()

    # Which declared predecessors fired within the sequence window.
    window = timedelta(minutes=space.predecessor_window_minutes)
    fired = sorted(
        (e.available_at, e.type) for t in space.predecessors
        for e in events_by_type.get(t, []))
    for at in all_tags:
        cutoff = at - window
        recent_by_time[at] = frozenset(
            t for when, t in fired if cutoff <= when <= at)

    def third_of(session_date) -> int:
        return 0 if session_date < thirds[0] else (
            1 if session_date < thirds[1] else 2)

    def tag_for(at, session_date):
        tags = all_tags.get(at)
        if tags is None:
            return None
        return tags, recent_by_time.get(at, frozenset()), third_of(session_date)

    index = BarWindowIndex(stage_bars)
    pool = arms.build_control_pool(set())
    print(f"\n4. measuring forward paths "
          f"({len(pool.anchors()):,} control anchors)")

    def to_tagged(anchor_at, session_date, stratum_key, path):
        tagged = tag_for(anchor_at, session_date)
        if tagged is None or path.bars_observed == 0:
            return None
        tags, recent, third = tagged
        return Tagged(
            stratum_key=stratum_key, session_date=session_date.isoformat(),
            third=third, tags=tags, recent=recent,
            signed_return=float(path.terminal_return),
            max_up=float(path.max_up), max_down=float(path.max_down),
            favorable_first_up=(None if path.max_up_at is None
                                or path.max_down_at is None
                                or path.max_up_at == path.max_down_at
                                else path.max_up_at < path.max_down_at))

    controls_by_horizon = {}
    events_by_anchor = {}
    prep = StageProgress("discovery/paths", len(space.horizons),
                         unit="horizons", min_interval=0.0,
                         counter_labels=("anchors", "paths"))
    for horizon in space.horizons:
        measured = measure_anchor_paths(pool.anchors(), stage_bars, horizon,
                                        index)
        controls_by_horizon[horizon] = [
            t for t in (to_tagged(a.at, a.session_date, a.stratum.as_key(), p)
                        for a, p, _s in measured) if t is not None]
        for anchor_type in space.anchors:
            anchors = []
            for e in events_by_type.get(anchor_type, []):
                a = arms.anchor_at(e.available_at)
                if a is not None:
                    anchors.append(a)
            ms = measure_anchor_paths(anchors, stage_bars, horizon, index)
            events_by_anchor[(anchor_type, horizon)] = [
                t for t in (to_tagged(a.at, a.session_date,
                                      a.stratum.as_key(), p)
                            for a, p, _s in ms) if t is not None]
        prep.advance(len(pool.anchors()), len(controls_by_horizon[horizon]))
    prep.finish()

    print(f"\n5. screening the full census of {len(census):,} candidates ...")
    started = time.perf_counter()
    screened = screen_candidates(census, events_by_anchor,
                                 controls_by_horizon, criteria, space)
    survivors = [s for s in screened if s.passed]
    print(f"   {len(survivors):,} passed the screen "
          f"({format_duration(time.perf_counter() - started)})")

    survivors.sort(key=lambda s: (-abs(s.lift or 0), s.candidate.key()))
    shortlist = survivors[:criteria.shortlist_size]
    print(f"\n6. full assessment of the top {len(shortlist)} "
          f"(stability first, inference last)")
    assessments = []
    detail = StageProgress("discovery/assess", max(1, len(shortlist)),
                           unit="candidates", min_interval=0.0,
                           counter_labels=("controls", "events"))
    for s in shortlist:
        a = assess(s.candidate, events_by_anchor[(s.candidate.anchor,
                                                  s.candidate.horizon_minutes)],
                   controls_by_horizon[s.candidate.horizon_minutes],
                   criteria, matching, len(census))
        assessments.append(a)
        detail.advance(a.n_controls, a.n_events)
    detail.finish()

    winner = select_winner(assessments)
    print("\n" + "=" * 72)
    if winner is None:
        print("NO VIABLE CANDIDATE")
        print("=" * 72)
        print(f"\n{len(census):,} candidates evaluated; "
              f"{len(survivors):,} passed the screen; none cleared the frozen")
        print("bar. The most common reasons among the shortlist:\n")
        counts = {}
        for a in assessments:
            for f in a.failures:
                counts[f.split(":")[0]] = counts.get(f.split(":")[0], 0) + 1
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"   {n:>4}  {reason}")
    else:
        c = winner.candidate
        print("CANDIDATE FOUND")
        print("=" * 72)
        print(f"\nH13\nWhen:       {c.describe()}")
        print(f"Horizon:    {c.horizon_minutes} minutes")
        print(f"Complexity: {c.complexity} elements\n")
        print(f"   events            {winner.n_events:,}")
        print(f"   sessions          {winner.event_sessions:,}")
        print(f"   effective clusters{winner.effective_clusters:>8.1f}")
        print(f"   event mean        {winner.event_mean:+.3f}")
        print(f"   baseline mean     {winner.control_mean:+.3f}")
        print(f"   lift              {winner.lift:+.3f}")
        print(f"   clustered 95% CI  [{winner.lift_ci_low:+.3f}, "
              f"{winner.lift_ci_high:+.3f}]   p={winner.lift_p_value:.4g}")
        print(f"   thirds            {[None if t is None else round(t, 3) for t in winner.thirds]}")
        print(f"   regimes           "
              f"{ {k: (None if v is None else round(v, 3)) for k, v in winner.regimes.items()} }")
        print(f"   drop-one-condition{ {k: (None if v is None else round(v, 3)) for k, v in winner.perturbed.items()} }")
        print(f"   score             {winner.score:.4f}")
        print(f"\n   selected from {winner.census_size:,} candidates. A "
              f"family-wise correction over")
        print(f"   that census would demand p <= "
              f"{winner.family_wise_alpha:.3g}; this candidate reports "
              f"p={winner.lift_p_value:.4g},")
        print(f"   and the bootstrap cannot express a p below "
              f"{winner.p_resolution_floor:.3g}. The interval above is")
        print("   SELECTION-INFLATED and is not evidence. Preregister it and "
              "test it")
        print("   on data this search never touched.")

    report = {
        "analysis_version": ANALYSIS_VERSION, "status": STATUS,
        "confirmatory": False, "preregistered": False,
        "partition_used": "discovery", "validation_read": False,
        "holdout_read": False,
        "search_version": SEARCH_VERSION,
        "search_space_digest": space.digest(),
        "criteria_digest": criteria.digest(),
        "matching_digest": matching.digest(),
        "census_size": len(census),
        "ci_confidence": criteria.ci_confidence,
        "family_wise_alpha_reference":
            criteria.reported_family_wise_alpha / len(census),
        "screened_passed": len(survivors),
        "shortlist_size": len(shortlist),
        "manifest": manifest,
        "winner": winner.as_row() if winner else None,
        "assessments": [a.as_row() for a in assessments],
        "census": [s.as_row() for s in screened],
    }
    out_path = Path(args.out)
    tmp = out_path.with_suffix(out_path.suffix + ".partial")
    payload = json.dumps(report, indent=2, sort_keys=True, default=str)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, out_path)
    print(f"\ncomplete search log written to {out_path} "
          f"({len(payload):,} bytes) -- every candidate, including those")
    print("that failed a gate, so the amount of mining is auditable.")
    print(f"\n{ANALYSIS_VERSION}: {STATUS}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
