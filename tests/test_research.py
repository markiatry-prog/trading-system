"""T-004 Phase A: the research framework.

These tests are mostly about the guardrails, because the guardrails are
the deliverable. A research framework that produces numbers is easy; one
that makes it hard to fool yourself is the point.
"""
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures_market import NQ, RTH_OPEN, bar  # noqa: E402
from trading_system.features.records import (  # noqa: E402
    FeatureRecord, RecordKind)
from trading_system.research.classify import Verdict, classify  # noqa: E402
from trading_system.research.hypotheses import (  # noqa: E402
    Direction, HypothesisRegistry, RegistryError)
from trading_system.research.inference import (  # noqa: E402
    MIN_SAMPLES_FOR_ANY_CLAIM, bootstrap_mean, minimum_n)
from trading_system.research.multiplicity import (  # noqa: E402
    SnoopingLedger, benjamini_hochberg, bonferroni)
from trading_system.research.outcomes import measure_forward_path  # noqa: E402
from trading_system.research.partitions import (  # noqa: E402
    ChronologicalPartitions, HoldoutSealed, Partition, split_chronologically)
from trading_system.research.quality import QualityReport, assess_day  # noqa: E402
from trading_system.research.study import (  # noqa: E402
    Study, StudyError, condition_state, matches)

DAYS = [date(2026, 1, 5) + timedelta(days=i) for i in range(100)]
T0 = datetime(2026, 1, 12, 15, 0, tzinfo=timezone.utc)


def _bars(specs, origin=T0):
    out = []
    for i, (o, h, l, c) in enumerate(specs):
        t = origin + timedelta(minutes=i)
        out.append(bar(0, o, h, l, c, origin=t))
    return out


# --- partitions -------------------------------------------------------

def test_partitions_are_chronological_and_non_overlapping():
    p = split_chronologically(DAYS)
    assert p.discovery[1] < p.validation[0] < p.validation[1] < p.holdout[0]


def test_overlapping_partitions_are_refused():
    with pytest.raises(ValueError, match="overlaps"):
        ChronologicalPartitions(discovery=(date(2026, 1, 1), date(2026, 2, 1)),
                                validation=(date(2026, 1, 15), date(2026, 3, 1)),
                                holdout=(date(2026, 3, 2), date(2026, 4, 1)))


def test_holdout_is_sealed_and_refuses_access():
    p = split_chronologically(DAYS)
    assert p.is_sealed
    with pytest.raises(HoldoutSealed, match="sealed"):
        p.select(DAYS, Partition.HOLDOUT)
    # the other two are freely available
    assert p.select(DAYS, Partition.DISCOVERY)
    assert p.select(DAYS, Partition.VALIDATION)


def test_unsealing_requires_a_substantive_reason_and_is_recorded_forever():
    p = split_chronologically(DAYS)
    with pytest.raises(ValueError, match="substantive"):
        p.unseal("because")
    p.unseal("hypotheses frozen and validation complete; final evaluation")
    assert p.select(DAYS, Partition.HOLDOUT)
    assert p.provenance()["holdout_ever_unsealed"] is True
    p.reseal()
    # resealing does NOT erase the evidence that it was opened
    assert p.provenance()["holdout_ever_unsealed"] is True


# --- pre-registration -------------------------------------------------

def _registry():
    r = HypothesisRegistry()
    r.register("H1", "ORB break continues up", "opening_range_high_broken",
               Direction.UP, 30, {}, "median forward return <= 0 or CI spans zero")
    return r


def test_hypothesis_needs_a_falsifier():
    r = HypothesisRegistry()
    with pytest.raises(RegistryError, match="falsifier"):
        r.register("H", "something", "e", Direction.UP, 10, {}, "no")


def test_registry_is_hash_chained_and_detects_tampering():
    import dataclasses
    r = _registry()
    r.register("H2", "second", "e2", Direction.DOWN, 15, {}, "CI spans zero here")
    assert r.verify()
    r._entries[0] = dataclasses.replace(r._entries[0], statement="rewritten later")
    assert not r.verify(), "an edited hypothesis must break the chain"


def test_no_hypotheses_after_sealing():
    r = _registry()
    r.seal()
    with pytest.raises(RegistryError, match="sealed"):
        r.register("H9", "late idea", "e", Direction.UP, 10, {}, "a real falsifier")


def test_results_cannot_attach_to_an_unregistered_hypothesis():
    r = _registry()
    with pytest.raises(RegistryError, match="never pre-registered"):
        r.get("H_INVENTED_AFTER_SEEING_RESULTS")


# --- data quality -----------------------------------------------------
#
# The gate was redesigned after the first real-data run excluded 695 of
# 1,256 sessions on a heuristic that mis-read normal market structure.
# It is now tested in full in tests/test_quality_gate.py, against the
# five-way distinction between legitimate silence, session structure,
# early closes, true feed gaps and known degraded dates. The report's
# role in a study is what is checked here.


def test_the_report_supplies_an_honest_denominator_to_a_study():
    from trading_system.features.calendar import SessionCalendar
    from trading_system.features.config import SessionSpec
    from trading_system.research.quality import assess_day
    calendar = SessionCalendar(SessionSpec())
    report = QualityReport()
    for i in range(4):
        report.add(assess_day(date(2026, 1, 12), "NQZ6", [], calendar))
    s = report.summary()
    assert s["days_assessed"] == 4
    assert s["days_passed"] == 0
    assert s["exclusion_reasons"], "an exclusion must state its rule"


# --- forward paths ----------------------------------------------------

def test_reference_is_the_first_bar_closing_at_or_after_the_event():
    bars = _bars([(100, 101, 99, 100), (100, 105, 99, 104), (104, 106, 103, 105)])
    path, _ = measure_forward_path("e", bars[0].closed_at, bars, 30)
    assert path.reference_price == bars[0].close
    assert path.bars_observed == 2


def test_terminal_return_hides_what_excursion_ordering_reveals():
    """Identical terminal return; opposite tradability."""
    down_first = _bars([(100, 100, 100, 100), (100, 101, 70, 75), (75, 130, 75, 120)])
    up_first = _bars([(100, 100, 100, 100), (100, 130, 99, 120), (120, 121, 70, 120)])
    a, sa = measure_forward_path("e", down_first[0].closed_at, down_first, 30)
    b, sb = measure_forward_path("e", up_first[0].closed_at, up_first, 30)
    assert a.terminal_return == b.terminal_return == Decimal(20)
    assert a.favorable_came_first(Direction.UP) is False
    assert b.favorable_came_first(Direction.UP) is True
    assert sa.hit_first(Direction.UP, Decimal(20), Decimal(10))[0] == "adverse"
    assert sb.hit_first(Direction.UP, Decimal(20), Decimal(10))[0] == "favorable"


def test_both_thresholds_inside_one_bar_is_reported_as_unknown():
    """Calling this a win would be the most flattering lie available."""
    bars = _bars([(100, 100, 100, 100), (100, 130, 70, 100)])
    _, scanner = measure_forward_path("e", bars[0].closed_at, bars, 30)
    assert scanner.hit_first(Direction.UP, Decimal(20), Decimal(10))[0] == "same_bar"


def test_mfe_and_mae_flip_with_direction():
    bars = _bars([(100, 100, 100, 100), (100, 110, 90, 105)])
    path, _ = measure_forward_path("e", bars[0].closed_at, bars, 30)
    assert path.mfe(Direction.UP) == Decimal(10)
    assert path.mae(Direction.UP) == Decimal(10)
    assert path.mfe(Direction.DOWN) == Decimal(10)
    assert path.signed_return(Direction.UP) == Decimal(5)
    assert path.signed_return(Direction.DOWN) == Decimal(-5)


def test_no_actionable_bar_returns_none_rather_than_guessing():
    bars = _bars([(100, 101, 99, 100)])
    assert measure_forward_path("e", bars[0].closed_at + timedelta(hours=5),
                                bars, 30) is None


# --- conditioning uses available_at -----------------------------------

def _feature(ftype, effective, available, state=None, value=None):
    return FeatureRecord(instrument_symbol="NQZ6", kind=RecordKind.FEATURE,
                         type=ftype, effective_at=effective,
                         available_at=available, session_date="2026-01-12",
                         state=state, value=value)


def test_conditioning_uses_availability_not_occurrence():
    """A fact that had happened but was not yet confirmed must be
    invisible. This single function is where lookahead would enter the
    conditioning layer."""
    late = _feature("swing_high", effective=T0, available=T0 + timedelta(minutes=5),
                    value=Decimal(100))
    assert condition_state([late], "swing_high", T0) is None
    assert condition_state([late], "swing_high", T0 + timedelta(minutes=5)) is not None


def test_most_recent_knowable_state_wins():
    a = _feature("above_vwap", T0, T0, state="below")
    b = _feature("above_vwap", T0 + timedelta(minutes=1),
                 T0 + timedelta(minutes=1), state="above")
    assert condition_state([a, b], "above_vwap", T0 + timedelta(minutes=2)) == "above"
    assert condition_state([a, b], "above_vwap", T0) == "below"


def test_matches_requires_every_condition():
    recs = [_feature("above_vwap", T0, T0, state="above")]
    assert matches(recs, {"above_vwap": "above"}, T0 + timedelta(minutes=1))
    assert not matches(recs, {"above_vwap": "below"}, T0 + timedelta(minutes=1))
    assert not matches(recs, {"missing_feature": "x"}, T0 + timedelta(minutes=1))


# --- multiplicity -----------------------------------------------------

def test_benjamini_hochberg_matches_the_hand_computation():
    ps = [("a", 0.001), ("b", 0.02), ("c", 0.04), ("d", 0.30), ("e", 0.80)]
    out = {c.label: c for c in benjamini_hochberg(ps, fdr=0.05)}
    assert out["a"].significant and out["b"].significant
    assert not out["c"].significant       # 0.04 > (3/5)*0.05 = 0.03
    assert out["a"].p_adjusted == pytest.approx(0.005)


def test_bonferroni_is_stricter_than_bh():
    ps = [("a", 0.01), ("b", 0.02), ("c", 0.03), ("d", 0.04)]
    bh = sum(c.significant for c in benjamini_hochberg(ps))
    bf = sum(c.significant for c in bonferroni(ps))
    assert bf <= bh


def test_the_ledger_counts_exploratory_tests_too():
    """A ledger counting only reported tests would relocate the
    self-deception, not remove it."""
    led = SnoopingLedger()
    for i in range(10):
        led.record(f"t{i}", exploratory=i >= 6)
    assert led.total_tests == 10 and led.exploratory_tests == 4


# --- classification ---------------------------------------------------

def _est(n, mean, lo, hi):
    from trading_system.research.inference import Estimate
    return Estimate(n=n, mean=mean, median=mean, stdev=1.0, ci_low=lo,
                    ci_high=hi, confidence=0.95, seed=1)


def test_underpowered_is_inconclusive_never_reject():
    """The most important rule here. 'No significant effect' on 12
    observations is not evidence of no effect."""
    c = classify(_est(12, 0.1, -0.5, 0.7))
    assert c.verdict is Verdict.INCONCLUSIVE
    assert "could not have detected" in c.reason


def test_adequately_powered_null_is_a_reject_and_that_is_a_success():
    c = classify(_est(500, 0.01, -0.20, 0.22))
    assert c.verdict is Verdict.REJECT
    assert "real finding" in c.reason


def test_significant_but_failing_multiplicity_is_rejected():
    c = classify(_est(500, 1.0, 0.5, 1.5), survives_multiplicity=False)
    assert c.verdict is Verdict.REJECT
    assert "chance" in c.reason


def test_discovery_only_is_promising_never_robust():
    c = classify(_est(500, 1.0, 0.5, 1.5), survives_multiplicity=True)
    assert c.verdict is Verdict.PROMISING


def test_opposite_directions_across_periods_is_rejected():
    c = classify(_est(500, 1.0, 0.5, 1.5), _est(500, -1.0, -1.5, -0.5),
                 survives_multiplicity=True)
    assert c.verdict is Verdict.REJECT
    assert "OPPOSITE" in c.reason


def test_failing_the_holdout_is_rejected():
    c = classify(_est(500, 1.0, 0.5, 1.5), _est(500, 0.9, 0.4, 1.4),
                 _est(500, 0.05, -0.4, 0.5), survives_multiplicity=True)
    assert c.verdict is Verdict.REJECT
    assert "holdout is for" in c.reason


def test_robust_requires_all_three_periods():
    c = classify(_est(500, 1.0, 0.5, 1.5), _est(500, 0.9, 0.4, 1.4),
                 _est(500, 0.8, 0.3, 1.3), survives_multiplicity=True, tradable=True)
    assert c.verdict is Verdict.ROBUST


def test_statistically_robust_but_untradable_stops_at_promising():
    """Predictive is not tradable. The ordering decides."""
    c = classify(_est(500, 1.0, 0.5, 1.5), _est(500, 0.9, 0.4, 1.4),
                 _est(500, 0.8, 0.3, 1.3), survives_multiplicity=True, tradable=False)
    assert c.verdict is Verdict.PROMISING
    assert "not capturable" in c.reason


# --- study orchestration ----------------------------------------------

def test_testing_before_sealing_the_registry_is_refused():
    study = Study("s", split_chronologically(DAYS), _registry(), QualityReport())
    with pytest.raises(StudyError, match="seal"):
        study.test("H1", Partition.DISCOVERY, [], [], [])


def test_study_provenance_is_complete_and_reproducible():
    r = _registry(); r.seal()
    p = split_chronologically(DAYS)
    a = Study("s", p, r, QualityReport())
    b = Study("s", p, r, QualityReport())
    assert a.reproducibility_digest() == b.reproducibility_digest()
    prov = a.provenance()
    for key in ("study_version", "partitions", "hypotheses", "data_quality",
                "multiplicity", "results"):
        assert key in prov
    assert prov["hypotheses"]["chain_valid"] is True
