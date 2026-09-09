"""The T-005 search: frozen before the data, deterministic after it.

A search that looks at outcomes and then decides how to rank them is a
selection dressed as a search. These tests hold the harness to the
opposite: the space, the gates, the penalty and the score are fixed and
hashed before anything is measured, the winner falls out of a formula,
and validation and holdout cannot be reached at all.
"""
import ast
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from trading_system.research.baseline import MatchingSpec
from trading_system.research.discovery import (
    ANCHOR_EVENTS, PREDECESSOR_EVENTS, SEARCH_VERSION, TAG_FAMILIES,
    TAG_INDEX, TAG_ORDER, TAG_VALUES, Assessment, Candidate, SearchSpace,
    SelectionCriteria, Tagged, assess, enumerate_candidates,
    screen_candidates, select_winner)

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_t005_search.py"
SPACE = SearchSpace()
CRITERIA = SelectionCriteria()
FAST = MatchingSpec(bootstrap_iterations=300, diagnostic_iterations=50,
                    permutation_iterations=50, min_sessions_for_inference=5)


# --- frozen before the data -------------------------------------------

def test_the_census_is_enumerable_without_any_data():
    """If the space needed outcomes to enumerate, it would not be frozen."""
    census = enumerate_candidates(SPACE)
    assert len(census) > 1000
    assert len({c.key() for c in census}) == len(census)
    assert census == enumerate_candidates(SPACE)


def test_the_digests_change_when_the_space_or_criteria_change():
    assert SPACE.digest() == SearchSpace().digest()
    assert SPACE.digest() != replace(SPACE, horizons=(30,)).digest()
    assert SPACE.digest() != replace(SPACE, max_conditions=1).digest()
    assert CRITERIA.digest() == SelectionCriteria().digest()
    assert CRITERIA.digest() != replace(CRITERIA, min_events=10).digest()
    assert CRITERIA.digest() != replace(
        CRITERIA, complexity_penalty_per_element="0.99").digest()


def test_complexity_is_capped_and_conditions_never_repeat_a_family():
    for c in enumerate_candidates(SPACE):
        assert c.complexity <= SPACE.max_complexity
        families = [f for f, _ in c.conditions]
        assert len(families) == len(set(families)), c.key()
        if c.predecessor:
            assert len(c.conditions) <= SPACE.max_conditions_with_predecessor
        else:
            assert len(c.conditions) <= SPACE.max_conditions


def test_session_open_and_close_cannot_anchor_a_candidate():
    """They are bookkeeping, not market events."""
    assert "session_open" not in ANCHOR_EVENTS
    assert "session_close" not in ANCHOR_EVENTS


def test_every_tag_family_is_used_and_every_value_reachable():
    census = enumerate_candidates(SPACE)
    seen = {(f, v) for c in census for f, v in c.conditions}
    for family, values in TAG_FAMILIES:
        for value in values:
            assert (family, value) in seen, f"{family}={value} unreachable"


# --- structural inaccessibility ---------------------------------------

def test_the_runner_names_only_the_discovery_partition():
    tree = ast.parse(RUNNER.read_text())
    referenced = {n.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute)
                  and isinstance(n.value, ast.Name) and n.value.id == "Partition"}
    assert referenced == {"DISCOVERY"}, referenced


def test_the_runner_has_no_flag_that_could_reach_another_partition():
    tree = ast.parse(RUNNER.read_text())
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            flags.update(a.value for a in node.args
                         if isinstance(a, ast.Constant))
    for banned in ("unseal", "holdout", "validation", "partition"):
        assert not any(banned in f for f in flags), banned


def test_the_runner_never_calls_unseal():
    tree = ast.parse(RUNNER.read_text())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "unseal" not in called


def test_the_report_records_which_partitions_were_read():
    source = RUNNER.read_text()
    assert '"validation_read": False' in source
    assert '"holdout_read": False' in source
    assert '"partition_used": "discovery"' in source


def test_the_search_is_labelled_as_generation_not_evidence():
    source = RUNNER.read_text()
    assert '"confirmatory": False' in source
    assert '"preregistered": False' in source
    assert "selection-inflated" in source


# --- deterministic selection ------------------------------------------

def _assessment(key_suffix, score, clusters=100.0):
    c = Candidate("displacement_up", (), None, 30, "up")
    a = Assessment(candidate=c, qualified=True, score=score,
                   effective_clusters=clusters)
    # Distinguish by key without touching the frozen Candidate shape.
    object.__setattr__(a, "candidate",
                       Candidate("displacement_up", (), key_suffix, 30, "up"))
    return a


def test_no_qualified_candidate_means_no_winner():
    assert select_winner([]) is None
    losing = Assessment(candidate=Candidate("displacement_up", (), None, 30,
                                            "up"), qualified=False, score=9.9)
    assert select_winner([losing]) is None


def test_the_winner_is_the_highest_score():
    a = _assessment("displacement_down", 1.0)
    b = _assessment("structure_break_up", 5.0)
    assert select_winner([a, b]) is b
    assert select_winner([b, a]) is b          # order must not matter


def test_ties_break_deterministically():
    a = _assessment("displacement_down", 2.0, clusters=100.0)
    b = _assessment("structure_break_up", 2.0, clusters=200.0)
    assert select_winner([a, b]) is b          # more clusters wins
    c = _assessment("displacement_down", 2.0, clusters=200.0)
    d = _assessment("structure_break_up", 2.0, clusters=200.0)
    assert select_winner([c, d]) is c          # then the key, ascending
    assert select_winner([d, c]) is c


def test_a_simpler_candidate_beats_a_slightly_better_complex_one():
    """The complexity penalty, exercised rather than asserted."""
    penalty = float(CRITERIA.complexity_penalty_per_element)
    simple = abs(10.0) * penalty ** 0        # complexity 1
    complex_ = abs(11.0) * penalty ** 2      # complexity 3, 10% better lift
    assert simple > complex_, (
        "a 10% better fit is buying two extra conditions; the penalty must "
        "not permit that")


# --- the screen -------------------------------------------------------

def tagged(n, lift, session_offset=0, tags=None, third=0, recent=frozenset()):
    base = tags or ("open30", "normal", "normal", "above", "inside_or", "none")
    return [Tagged(stratum_key=(0, "normal"),
                   session_date=(date(2021, 9, 7)
                                 + timedelta(days=session_offset + i)).isoformat(),
                   third=third, tags=base, recent=recent, signed_return=lift,
                   max_up=max(lift, 0.0) + 5.0, max_down=min(lift, 0.0) - 1.0,
                   favorable_first_up=True)
            for i in range(n)]


def test_the_screen_counts_every_candidate_including_failures():
    census = enumerate_candidates(SPACE)[:200]
    results = screen_candidates(census, {}, {}, CRITERIA, SPACE)
    assert len(results) == len(census), "the census must be fully accounted for"
    assert all(not r.passed for r in results)
    assert all(r.reason for r in results)


def test_the_screen_rejects_movement_against_the_stated_direction():
    c = Candidate("displacement_up", (), None, 30, "up")
    events = tagged(400, -5.0)
    controls = tagged(400, 0.0, session_offset=500)
    r = screen_candidates([c], {("displacement_up", 30): events},
                          {30: controls}, CRITERIA, SPACE)[0]
    assert not r.passed
    assert "against the stated direction" in r.reason


def test_the_screen_rejects_an_economically_trivial_lift():
    c = Candidate("displacement_up", (), None, 30, "up")
    events = tagged(400, 0.05)
    controls = tagged(400, 0.0, session_offset=500)
    r = screen_candidates([c], {("displacement_up", 30): events},
                          {30: controls}, CRITERIA, SPACE)[0]
    assert not r.passed and "economic floor" in r.reason


def test_the_screen_rejects_a_rare_relationship():
    c = Candidate("displacement_up", (), None, 30, "up")
    events = tagged(CRITERIA.min_events - 1, 50.0)
    controls = tagged(400, 0.0, session_offset=500)
    r = screen_candidates([c], {("displacement_up", 30): events},
                          {30: controls}, CRITERIA, SPACE)[0]
    assert not r.passed and "event floor" in r.reason


# --- full assessment --------------------------------------------------

def controls_across_thirds(n, offset, tags=None):
    """Controls must exist in every third, or a chronological check
    fails for want of a comparator rather than for want of stability."""
    out = []
    for third in (0, 1, 2):
        out += tagged(n, 0.0, session_offset=offset + third * n, tags=tags,
                      third=third)
    return out


def test_a_planted_effect_is_recovered_and_qualifies():
    """The harness must be able to find something when something is
    there, or its silence on real data would mean nothing."""
    events = (tagged(200, 6.0, third=0)
              + tagged(200, 6.0, session_offset=200, third=1)
              + tagged(200, 6.0, session_offset=400, third=2))
    for regime in ("quiet", "normal", "elevated"):
        events += tagged(80, 6.0, session_offset=600,
                         tags=("open30", regime, "normal", "above",
                               "inside_or", "none"), third=0)
    controls = []
    for i, regime in enumerate(("quiet", "normal", "elevated")):
        controls += controls_across_thirds(
            150, 1000 + i * 500,
            tags=("open30", regime, "normal", "above", "inside_or", "none"))
    c = Candidate("displacement_up", (), None, 30, "up")
    a = assess(c, events, controls, CRITERIA, FAST, census_size=10)
    assert a.failures == [], a.failures
    assert a.qualified
    assert a.lift == pytest.approx(6.0, abs=0.5)
    assert a.lift_ci_low > 0
    assert select_winner([a]) is a


def test_an_effect_confined_to_one_third_is_rejected():
    """Chronological instability is disqualifying, however strong."""
    events = tagged(600, 30.0, third=0)
    controls = controls_across_thirds(300, 1000)
    c = Candidate("displacement_up", (), None, 30, "up")
    a = assess(c, events, controls, CRITERIA, FAST, census_size=10)
    assert not a.qualified
    assert any("third" in f for f in a.failures)


def test_an_effect_from_too_few_sessions_is_rejected():
    """One unusual week cannot become a candidate however large."""
    few = [replace(t, session_date="2021-09-07", third=i % 3)
           for i, t in enumerate(tagged(900, 20.0))]
    controls = controls_across_thirds(300, 1000)
    c = Candidate("displacement_up", (), None, 30, "up")
    a = assess(c, few, controls, CRITERIA, FAST, census_size=10)
    assert not a.qualified
    assert "min_event_sessions" in a.failures
    assert "min_effective_clusters" in a.failures


def test_a_knife_edge_condition_is_rejected():
    """The effect exists only inside one exact cell; widening the
    condition destroys it. That is a fit, not a behaviour."""
    inside = tagged(300, 20.0, tags=("open30", "elevated", "normal", "above",
                                     "inside_or", "none"), third=0)
    inside += tagged(300, 20.0, session_offset=300,
                     tags=("open30", "elevated", "normal", "above",
                           "inside_or", "none"), third=1)
    inside += tagged(300, 20.0, session_offset=600,
                     tags=("open30", "elevated", "normal", "above",
                           "inside_or", "none"), third=2)
    outside = tagged(3000, -20.0, session_offset=900,
                     tags=("open30", "quiet", "normal", "above",
                           "inside_or", "none"))
    controls = controls_across_thirds(
        400, 5000, tags=("open30", "elevated", "normal", "above",
                         "inside_or", "none"))
    controls += controls_across_thirds(
        400, 9000, tags=("open30", "quiet", "normal", "above",
                         "inside_or", "none"))
    c = Candidate("displacement_up", (("atr_regime", "elevated"),), None,
                  30, "up")
    a = assess(c, inside + outside, controls, CRITERIA, FAST, census_size=10)
    assert not a.qualified
    assert any("parameter_sensitivity" in f for f in a.failures)


def test_a_family_wise_threshold_would_be_unreachable_and_is_not_a_gate():
    """The flaw this design avoids.

    Bonferroni over a census of ~14,000 demands a p near 4e-6. A
    percentile bootstrap cannot report a p below 2/(iterations+1), which
    at any affordable number of resamples is around 1e-3. Gating on the
    corrected alpha would therefore make the harness incapable of ever
    returning a candidate WHATEVER the data said -- and it would say NO
    VIABLE CANDIDATE with the confident air of a finding.

    So the census size is reported, never gated on: the correction it
    implies, the bootstrap's own resolution floor, and whether the
    winner would have survived. What controls the mining is the
    stability requirements, which the other tests exercise.
    """
    events = (tagged(200, 2.0, third=0) + tagged(200, 2.0, 200, third=1)
              + tagged(200, 2.0, 400, third=2))
    controls = controls_across_thirds(300, 1000)
    c = Candidate("displacement_up", (), None, 30, "up")
    small = assess(c, events, controls, CRITERIA, FAST, census_size=1)
    large = assess(c, events, controls, CRITERIA, FAST, census_size=10_000_000)

    # The measurement cannot depend on how many other things were tried.
    assert small.lift == large.lift
    assert small.qualified == large.qualified

    # But the context must be recorded, and must say the winner is far
    # from surviving a family-wise correction.
    row = large.as_row()
    assert row["census_size"] == 10_000_000
    assert row["family_wise_alpha"] == pytest.approx(5e-9)
    assert row["survives_family_wise"] is False
    assert row["p_resolution_floor"] > row["family_wise_alpha"], (
        "the correction is below what the bootstrap can express, which is "
        "exactly why it must not be a gate")


def test_the_interval_gate_is_reachable_at_the_configured_confidence():
    """The gate that replaced it must actually be passable."""
    events = (tagged(200, 6.0, third=0) + tagged(200, 6.0, 200, third=1)
              + tagged(200, 6.0, 400, third=2))
    controls = controls_across_thirds(300, 1000)
    c = Candidate("displacement_up", (), None, 30, "up")
    a = assess(c, events, controls, CRITERIA, FAST, census_size=13_884)
    assert a.qualified, a.failures
    assert a.lift_ci_low > 0


def test_an_interval_including_zero_is_rejected():
    events = (tagged(100, 4.0, third=0) + tagged(100, -4.0, 100, third=0)
              + tagged(100, 4.0, 200, third=1) + tagged(100, -4.0, 300, third=1)
              + tagged(100, 4.0, 400, third=2) + tagged(100, -4.0, 500, third=2))
    controls = controls_across_thirds(300, 1000)
    c = Candidate("displacement_up", (), None, 30, "up")
    a = assess(c, events, controls, CRITERIA, FAST, census_size=100)
    assert not a.qualified


def test_assessment_serialises_for_the_search_log():
    c = Candidate("displacement_up", (("atr_regime", "quiet"),), None, 30, "up")
    a = Assessment(candidate=c)
    json.dumps(a.as_row(), default=str)
    assert a.as_row()["complexity"] == 2
    assert a.as_row()["key"] == c.key()


def test_a_candidate_describes_itself_in_plain_language():
    c = Candidate("liquidity_sweep_high", (("atr_regime", "quiet"),),
                  "displacement_up", 60, "down")
    text = c.describe()
    assert "liquidity_sweep_high" in text and "atr_regime is quiet" in text
    assert "displacement_up" in text and "falls" in text and "60" in text
