"""Positive, null, and reliably-opposite effects must be distinguished.

`signed_return` is oriented so positive means the PREREGISTERED
hypothesis was right. Classifying on `excludes_zero` alone threw that
sign away, so an interval lying entirely below zero -- the market moving
reliably AGAINST the prediction -- reached PROMISING and would have been
read as a confirmation.
"""
import pytest

from trading_system.research.classify import Classification, Verdict, classify
from trading_system.research.inference import (MIN_SAMPLES_FOR_ANY_CLAIM,
                                               Estimate)


def est(mean, ci_low, ci_high, n=500):
    return Estimate(n=n, mean=mean, median=mean, stdev=1.0, ci_low=ci_low,
                    ci_high=ci_high, confidence=0.95, seed=1)


POSITIVE = est(2.0, 1.2, 2.8)      # predicted effect, supported
NULL = est(0.05, -0.9, 1.0)        # interval crosses zero
OPPOSITE = est(-2.0, -2.8, -1.2)   # reliably wrong in the declared direction


# --- the three outcomes ----------------------------------------------

def test_a_supported_effect_is_promising():
    c = classify(POSITIVE, survives_multiplicity=True)
    assert c.verdict is Verdict.PROMISING


def test_an_interval_crossing_zero_is_rejected():
    c = classify(NULL, survives_multiplicity=True)
    assert c.verdict is Verdict.REJECT
    assert "includes zero" in c.reason


def test_a_reliably_opposite_effect_is_never_promising():
    """The regression. Before this verdict existed, OPPOSITE reached
    PROMISING and read as a confirmation of the hypothesis it refutes."""
    c = classify(OPPOSITE, survives_multiplicity=True)
    assert c.verdict is Verdict.OPPOSITE_EFFECT
    assert c.verdict is not Verdict.PROMISING


def test_the_opposite_verdict_refuses_to_endorse_the_reverse_strategy():
    c = classify(OPPOSITE, survives_multiplicity=True)
    assert "never preregistered" in c.reason
    assert "exploratory candidate" in c.reason


def test_an_opposite_effect_that_fails_multiplicity_is_just_noise():
    """Announcing a reversal that the correction does not support would
    manufacture a finding out of the same multiple comparisons the
    correction exists to absorb."""
    c = classify(OPPOSITE, survives_multiplicity=False)
    assert c.verdict is Verdict.REJECT


def test_an_underpowered_opposite_effect_is_inconclusive_not_a_reversal():
    small = est(-2.0, -2.8, -1.2, n=MIN_SAMPLES_FOR_ANY_CLAIM - 1)
    c = classify(small, survives_multiplicity=True)
    assert c.verdict is Verdict.INCONCLUSIVE


def test_power_is_still_checked_before_anything_else():
    for e in (POSITIVE, NULL, OPPOSITE):
        small = est(e.mean, e.ci_low, e.ci_high, n=5)
        assert classify(small, survives_multiplicity=True).verdict \
            is Verdict.INCONCLUSIVE


# --- the reversal cannot be promoted ----------------------------------

def test_an_opposite_effect_confirmed_out_of_sample_is_still_not_robust():
    """Consistency across periods makes the REVERSAL more credible, and
    still does not make it a tested hypothesis."""
    c = classify(OPPOSITE, validation=OPPOSITE, holdout=OPPOSITE,
                 survives_multiplicity=True)
    assert c.verdict is Verdict.OPPOSITE_EFFECT
    assert c.verdict is not Verdict.ROBUST
    assert "agrees in sign" in c.reason


def test_an_unstable_reversal_says_so():
    c = classify(OPPOSITE, validation=POSITIVE, survives_multiplicity=True)
    assert c.verdict is Verdict.OPPOSITE_EFFECT
    assert "does NOT agree" in c.reason


def test_a_supported_effect_still_reaches_robust_through_all_three():
    c = classify(POSITIVE, validation=POSITIVE, holdout=POSITIVE,
                 survives_multiplicity=True, tradable=True)
    assert c.verdict is Verdict.ROBUST


def test_a_supported_discovery_that_reverses_in_validation_is_rejected():
    """Distinct from OPPOSITE_EFFECT: discovery pointed the right way,
    so this is instability rather than a reversal."""
    c = classify(POSITIVE, validation=OPPOSITE, survives_multiplicity=True)
    assert c.verdict is Verdict.REJECT
    assert "OPPOSITE directions" in c.reason


# --- the boundary -----------------------------------------------------

@pytest.mark.parametrize("ci_low,ci_high,expected", [
    (0.1, 2.0, Verdict.PROMISING),          # entirely above
    (-0.1, 2.0, Verdict.REJECT),            # touches zero from above
    (-2.0, 0.1, Verdict.REJECT),            # touches zero from below
    (-2.0, -0.1, Verdict.OPPOSITE_EFFECT),  # entirely below
])
def test_the_sign_of_the_interval_decides(ci_low, ci_high, expected):
    mean = (ci_low + ci_high) / 2
    assert classify(est(mean, ci_low, ci_high),
                    survives_multiplicity=True).verdict is expected


def test_every_verdict_is_reachable_and_distinct():
    assert len({v.value for v in Verdict}) == len(list(Verdict)) == 5
    reached = {
        classify(POSITIVE, survives_multiplicity=True).verdict,
        classify(NULL, survives_multiplicity=True).verdict,
        classify(OPPOSITE, survives_multiplicity=True).verdict,
        classify(None).verdict,
        classify(POSITIVE, validation=POSITIVE, holdout=POSITIVE,
                 survives_multiplicity=True).verdict,
    }
    assert reached == set(Verdict)


def test_the_verdict_serialises():
    row = classify(OPPOSITE, survives_multiplicity=True).as_row()
    assert row["verdict"] == "opposite_effect"
