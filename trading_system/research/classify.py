"""REJECT / INCONCLUSIVE / PROMISING / ROBUST.

The classification is deliberately hard to reach the top of. A finding
becomes ROBUST only by surviving three chronologically separate periods
with multiplicity control and adequate power at each -- which is a high
bar, and is meant to be, because the cost of a false ROBUST is building
a system on it.

THE ASYMMETRY IS INTENTIONAL. REJECT and INCONCLUSIVE are cheap to
reach; both are successful outcomes. An adequately powered null is a
real finding: it removes a candidate. INCONCLUSIVE is an admission that
the study could not answer the question, which is different from
answering it in the negative and must never be written up as such.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .inference import MIN_SAMPLES_FOR_ANY_CLAIM, Estimate


class Verdict(str, Enum):
    REJECT = "reject"              # adequately powered, no effect found
    INCONCLUSIVE = "inconclusive"  # the study could not answer it
    PROMISING = "promising"        # survived discovery, not yet confirmed
    ROBUST = "robust"              # survived discovery, validation and holdout


@dataclass(frozen=True)
class Classification:
    verdict: Verdict
    reason: str
    tradable: Optional[bool] = None

    def as_row(self) -> dict:
        return {"verdict": self.verdict.value, "reason": self.reason,
                "tradable": self.tradable}


def classify(discovery: Optional[Estimate],
             validation: Optional[Estimate] = None,
             holdout: Optional[Estimate] = None,
             survives_multiplicity: bool = False,
             tradable: Optional[bool] = None,
             min_n: int = MIN_SAMPLES_FOR_ANY_CLAIM) -> Classification:
    """Apply the rules in the only order that keeps them honest.

    Power is checked FIRST. Asking "was it significant?" of an
    underpowered sample and answering "no" produces a REJECT that the
    data never earned.
    """
    if discovery is None:
        return Classification(Verdict.INCONCLUSIVE, "no discovery estimate")

    if discovery.n < min_n:
        return Classification(
            Verdict.INCONCLUSIVE,
            f"discovery n={discovery.n} below the minimum {min_n}; the study "
            f"could not have detected an effect, so absence of one is not "
            f"evidence of absence")

    if not discovery.excludes_zero:
        return Classification(
            Verdict.REJECT,
            f"adequately powered (n={discovery.n}) and the confidence "
            f"interval [{discovery.ci_low:.3f}, {discovery.ci_high:.3f}] "
            f"includes zero. This is a real finding: the candidate is removed.")

    if not survives_multiplicity:
        return Classification(
            Verdict.REJECT,
            "significant before correction but not after; with the number of "
            "tests actually run, this is the result expected by chance")

    if validation is None:
        return Classification(
            Verdict.PROMISING,
            f"survived discovery (n={discovery.n}, CI excludes zero) and "
            f"multiplicity correction; not yet tested out of sample",
            tradable=tradable)

    if validation.n < min_n:
        return Classification(
            Verdict.INCONCLUSIVE,
            f"validation n={validation.n} below the minimum {min_n}; the "
            f"out-of-sample test was not powered to confirm or refute",
            tradable=tradable)

    if not validation.excludes_zero:
        return Classification(
            Verdict.REJECT,
            f"held in discovery and failed in validation (n={validation.n}, "
            f"CI [{validation.ci_low:.3f}, {validation.ci_high:.3f}]). The "
            f"discovery result was an artefact of that period.")

    # Direction must agree. Opposite signs in two periods is not a
    # surviving effect, however significant each looks alone.
    if (discovery.mean > 0) != (validation.mean > 0):
        return Classification(
            Verdict.REJECT,
            "discovery and validation are significant in OPPOSITE directions; "
            "this is instability, not an effect")

    if holdout is None:
        return Classification(
            Verdict.PROMISING,
            f"survived discovery and validation in the same direction "
            f"(n={discovery.n}/{validation.n}); the final holdout is still "
            f"sealed and must remain so until the hypothesis set is frozen",
            tradable=tradable)

    if holdout.n < min_n:
        return Classification(
            Verdict.INCONCLUSIVE,
            f"holdout n={holdout.n} below the minimum {min_n}", tradable=tradable)

    if not holdout.excludes_zero or (holdout.mean > 0) != (discovery.mean > 0):
        return Classification(
            Verdict.REJECT,
            f"failed on the final holdout (n={holdout.n}, CI "
            f"[{holdout.ci_low:.3f}, {holdout.ci_high:.3f}]). This is what "
            f"the holdout is for.", tradable=tradable)

    if tradable is False:
        return Classification(
            Verdict.PROMISING,
            "statistically robust across all three periods, but the excursion "
            "ordering says it is not capturable: the adverse excursion "
            "typically precedes the favorable one. Predictive, not tradable.",
            tradable=False)

    return Classification(
        Verdict.ROBUST,
        f"survived discovery (n={discovery.n}), validation (n={validation.n}) "
        f"and the final holdout (n={holdout.n}) in a consistent direction, "
        f"with multiplicity control",
        tradable=tradable)
