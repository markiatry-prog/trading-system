"""The optimized path must produce EXACTLY what the reference path does.

An optimization to a research pipeline is only admissible if it changes
runtime and nothing else. These tests are the evidence for that claim:
every one of them runs both paths over the same fixture and asserts
equality of the actual values, not of summary statistics.

Two accelerators are under test.

  BarWindowIndex   locates the forward window by bisection instead of by
                   scanning every bar for every event. It answers
                   "where", never "what".
  FeatureStateIndex  locates the conditioning record by bisection
                   instead of scanning every feature record.

Both are confined to selection. The code that computes a number is
shared, so the only thing that could differ is WHICH bars or WHICH
record were selected -- which is exactly what is asserted here.
"""
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_system.features.contracts import ContractTimeline
from trading_system.features.engine import FeatureEngine
from trading_system.features.records import FeatureRecord, RecordKind
from trading_system.market_data import Bar
from trading_system.research.outcomes import (BarWindowIndex, _select_window,
                                              measure_forward_path)
from trading_system.research.partitions import Partition, split_chronologically
from trading_system.research.preregistered import build_registry
from trading_system.research.quality import QualityReport
from trading_system.research.study import (FeatureStateIndex, Study,
                                           condition_state,
                                           required_condition_types)

from tests.fixtures_market import NQ, RTH_OPEN, bar, synthetic_sessions

# Full-length sessions on purpose: a short one starting at the 22:00 UTC
# session open never reaches the 14:30 RTH open, so it produces no
# opening range, no VWAP and therefore nothing for the conditioned
# hypothesis to discriminate. Three of them keeps the scanning path --
# which is quadratic, that being the point -- inside a few seconds.
SESSIONS = 3
MINUTES = 1397


@pytest.fixture(scope="module")
def workload():
    """One engine pass, shared by every test in this file."""
    days = synthetic_sessions(SESSIONS, minutes=MINUTES)
    bars = [b for i in sorted(days) for b in days[i]]
    engine = FeatureEngine(NQ)
    records = []
    for i in sorted(days):
        records.extend(engine.run(days[i]))
    events = [r for r in records if r.kind is RecordKind.EVENT]
    features = [r for r in records if r.kind is RecordKind.FEATURE]
    assert events and features, "fixture produced nothing to compare"
    return days, bars, events, features


# --- window selection -------------------------------------------------

def test_indexed_window_selection_matches_scanning_bar_for_bar(workload):
    """The single place the bar index is used, over every real event."""
    _days, bars, events, _features = workload
    index = BarWindowIndex(bars)
    assert index.monotonic
    compared = 0
    # Every fourth event, three horizons each: the scanning side is
    # O(events x bars), so comparing all of them would cost minutes to
    # prove what a few thousand windows already prove.
    for event in events[::4]:
        for horizon in (15, 30, 60):
            scanned = _select_window(bars, event.available_at, horizon, None)
            indexed = _select_window(bars, event.available_at, horizon, index)
            assert (scanned is None) == (indexed is None)
            if scanned is None:
                continue
            ref_a, path_a = scanned
            ref_b, path_b = indexed
            assert ref_a is ref_b
            assert path_a == path_b
            compared += 1
    assert compared > 500, f"only {compared} windows compared; too weak"


def test_forward_paths_are_identical_with_and_without_the_index(workload):
    _days, bars, events, _features = workload
    index = BarWindowIndex(bars)
    for event in events[::4]:
        a = measure_forward_path(event.type, event.available_at, bars, 30)
        b = measure_forward_path(event.type, event.available_at, bars, 30, index)
        assert (a is None) == (b is None)
        if a is None:
            continue
        assert a[0] == b[0], f"ForwardPath differs at {event.available_at}"
        assert a[1].bars == b[1].bars
        assert a[1].reference_price == b[1].reference_price


def test_a_non_monotonic_series_falls_back_instead_of_answering_wrongly():
    """Bars are ordered by observed_at; closed_at adds each bar's own
    interval, so mixed intervals can close out of order. The index must
    notice and stand down rather than bisect an unsorted list."""
    mixed = [
        bar(0, 20000, 20004, 19996, 20002, interval=3600),   # closes 15:30
        bar(1, 20002, 20006, 19998, 20004, interval=60),     # closes 14:32
    ]
    index = BarWindowIndex(mixed)
    assert not index.monotonic
    assert not index.usable_for(mixed)
    at = mixed[0].observed_at + timedelta(minutes=1)
    assert (_select_window(mixed, at, 30, index)
            == _select_window(mixed, at, 30, None))


def test_an_index_refuses_to_describe_a_different_sequence(workload):
    """Applying one sequence's index to another would measure a window
    that does not exist in it."""
    _days, bars, _events, _features = workload
    index = BarWindowIndex(bars)
    other = list(bars[:10])
    assert index.usable_for(bars)
    assert not index.usable_for(other)


# --- conditioning -----------------------------------------------------

def test_indexed_condition_lookup_matches_scanning_at_every_event(workload):
    _days, _bars, events, features = workload
    wanted = {f.type for f in features}
    index = FeatureStateIndex(wanted)
    index.extend(features)
    assert index.usable
    checked = 0
    for feature_type in sorted(wanted):
        for event in events[::40]:
            scanned = condition_state(features, feature_type,
                                      event.available_at, None)
            indexed = condition_state(features, feature_type,
                                      event.available_at, index)
            assert scanned == indexed, feature_type
            checked += 1
    assert checked > 500


def test_condition_lookup_agrees_before_during_and_after_the_series(workload):
    """Boundaries are where a bisection and a scan most easily part."""
    _days, _bars, _events, features = workload
    ftype = features[0].type
    index = FeatureStateIndex({ftype})
    index.extend(features)
    same_type = [f for f in features if f.type == ftype]
    probes = [same_type[0].available_at - timedelta(days=1),
              same_type[0].available_at - timedelta(microseconds=1),
              same_type[0].available_at,
              same_type[len(same_type) // 2].available_at,
              same_type[-1].available_at,
              same_type[-1].available_at + timedelta(days=1)]
    for at in probes:
        assert (condition_state(features, ftype, at, None)
                == condition_state(features, ftype, at, index)), at


def test_a_tie_selects_the_same_record_the_scan_would(workload):
    """The scan compares strictly greater-than, so among records sharing
    an availability time it keeps the FIRST. The index walks back over
    ties to match. Per-bar features cannot tie, so this is constructed."""
    at = datetime(2026, 1, 12, 15, 0, tzinfo=timezone.utc)
    tied = [
        FeatureRecord(instrument_symbol="NQZ6", kind=RecordKind.FEATURE,
                      type="above_vwap", effective_at=at, available_at=at,
                      session_date="2026-01-12", state=s)
        for s in ("first", "second", "third")
    ]
    index = FeatureStateIndex({"above_vwap"})
    index.extend(tied)
    assert condition_state(tied, "above_vwap", at, None) == "first"
    assert condition_state(tied, "above_vwap", at, index) == "first"


def test_out_of_order_features_make_the_index_stand_down():
    at = datetime(2026, 1, 12, 15, 0, tzinfo=timezone.utc)
    records = [
        FeatureRecord(instrument_symbol="NQZ6", kind=RecordKind.FEATURE,
                      type="above_vwap", effective_at=t, available_at=t,
                      session_date="2026-01-12", state=str(i))
        for i, t in enumerate([at, at - timedelta(minutes=5)])
    ]
    index = FeatureStateIndex({"above_vwap"})
    index.extend(records)
    assert not index.usable
    # Unusable means the scan runs, so the answer is still right.
    assert condition_state(records, "above_vwap", at, index) == \
        condition_state(records, "above_vwap", at, None)


def test_the_index_keeps_only_what_conditioning_can_read(workload):
    """The memory claim, asserted rather than assumed."""
    _days, _bars, _events, features = workload
    wanted = required_condition_types(build_registry())
    index = FeatureStateIndex(wanted)
    kept = index.extend(features)
    assert kept < len(features) / 5, (
        f"kept {kept} of {len(features)}; the narrowing is not happening")
    assert set(index.summary()["types"]) <= wanted


# --- the whole study --------------------------------------------------

def _study_both_ways(days, bars, events, features, registry):
    """The same twelve tests, once scanning and once indexed."""
    timeline = ContractTimeline.from_bars(bars, FeatureEngine(NQ).calendar)
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(len(days))]
    parts = split_chronologically(day_list)

    # accelerate=False forces the scanning path. Without it Study builds
    # a bar index of its own and this arm would compare the optimized
    # code against itself.
    scanning = Study("s", parts, registry, QualityReport(), contracts=timeline,
                     accelerate=False)
    for h in registry.all():
        scanning.test(h.id, Partition.DISCOVERY, events, features, bars)

    index = FeatureStateIndex(required_condition_types(registry))
    index.extend(features)
    indexed = Study("s", parts, registry, QualityReport(), contracts=timeline)
    bar_index = BarWindowIndex(bars)
    for h in registry.all():
        indexed.test(h.id, Partition.DISCOVERY, events, (), bars,
                     bar_index=bar_index, condition_index=index)
    return scanning, indexed


def test_every_hypothesis_result_is_identical(workload):
    days, bars, events, features = workload
    registry = build_registry()          # ONE registry: see the digest test
    scanning, indexed = _study_both_ways(days, bars, events, features, registry)
    a = [r.as_row() for r in scanning.results]
    b = [r.as_row() for r in indexed.results]
    assert json.dumps(a, sort_keys=True, default=str) == \
        json.dumps(b, sort_keys=True, default=str)


def test_the_reproducibility_digest_is_identical(workload):
    """The strongest single assertion available: one hash over every
    number, sample size, interval and eligibility record in the study."""
    days, bars, events, features = workload
    registry = build_registry()
    scanning, indexed = _study_both_ways(days, bars, events, features, registry)
    assert scanning.reproducibility_digest() == indexed.reproducibility_digest()


def test_the_verdicts_are_identical(workload):
    days, bars, events, features = workload
    registry = build_registry()
    scanning, indexed = _study_both_ways(days, bars, events, features, registry)
    a = {k: v.as_row() for k, v in scanning.classify_all().items()}
    b = {k: v.as_row() for k, v in indexed.classify_all().items()}
    assert json.dumps(a, sort_keys=True, default=str) == \
        json.dumps(b, sort_keys=True, default=str)


def test_a_conditioned_hypothesis_matches_the_same_events(workload):
    """H2 is the only conditioned hypothesis, so it is the only route the
    feature index takes through the study.

    A real opening-range break fires once per session, so three sessions
    give three events and they happen to be on the same side of VWAP --
    a comparison that would agree no matter what the index did. The
    events here are therefore synthesised across the whole session so
    that conditioning genuinely SPLITS them, which is the only state in
    which agreement means anything. Their type and the conditioning are
    H2's own; nothing about the hypothesis is altered.
    """
    days, bars, events, features = workload
    registry = build_registry()
    h2 = registry.get("H2")
    vwap_states = [f for f in features if f.type in h2.conditions]
    assert vwap_states, "fixture emitted no conditioning feature"

    probes = [
        FeatureRecord(instrument_symbol=f.instrument_symbol,
                      kind=RecordKind.EVENT, type=h2.event_type,
                      effective_at=f.available_at, available_at=f.available_at,
                      session_date=f.session_date)
        for f in vwap_states[::37]
    ]
    states = {condition_state(features, list(h2.conditions)[0],
                              p.available_at, None) for p in probes}
    assert len(states) > 1, (
        f"every probe sits on the same side of VWAP ({states}); conditioning "
        f"cannot discriminate, so agreement would prove nothing")

    scanning, indexed = _study_both_ways(days, bars, probes, features, registry)
    a = next(r for r in scanning.results if r.hypothesis_id == "H2")
    b = next(r for r in indexed.results if r.hypothesis_id == "H2")
    assert a.n_events == b.n_events == len(probes)
    assert a.n_matched == b.n_matched
    assert 0 < a.n_matched < a.n_events
    assert a.as_row() == b.as_row()


# --- the engine changes -----------------------------------------------

def test_pruning_inverted_gaps_changes_no_record():
    """The engine drops inverted FVGs instead of rescanning them. That
    is only admissible because such a gap can never emit again -- which
    this asserts by keeping them and comparing the output."""
    days = synthetic_sessions(3, minutes=MINUTES)

    engine = FeatureEngine(NQ)
    pruned = []
    for i in sorted(days):
        pruned.extend(engine.run(days[i]))

    # A second engine whose pruning is disabled by re-adding survivors.
    keeping = FeatureEngine(NQ)
    original_update = keeping._update_fvg_states
    retained = []

    def no_prune(bar, at):
        before = list(keeping._open_fvgs)
        out = original_update(bar, at)
        keeping._open_fvgs = before          # put the dead ones back
        retained.append(len(before))
        return out

    keeping._update_fvg_states = no_prune
    unpruned = []
    for i in sorted(days):
        unpruned.extend(keeping.run(days[i]))

    assert max(retained) > 50, "fixture never accumulated enough gaps"
    assert [r.as_row() for r in pruned] == [r.as_row() for r in unpruned]
