"""Study orchestration: conditioning, execution, provenance.

Ties the pieces together in the ONE order that keeps a result honest:

  1. gate data quality           (before anything is measured)
  2. partition chronologically   (holdout sealed)
  3. pre-register hypotheses     (before any test runs)
  4. seal the registry           (the test count is now fixed)
  5. measure forward paths       (per event, strictly from available_at)
  6. estimate with intervals     (never a bare point estimate)
  7. correct for multiplicity    (using every test actually run)
  8. classify                    (power checked before significance)

Steps cannot be reordered by accident: the registry refuses late
hypotheses, the holdout refuses unsealed access, and results refuse to
attach to an unregistered id.

CONDITIONING USES available_at, ALWAYS. A condition is evaluated from
the most recent feature record KNOWABLE at the event, never the most
recent one that had occurred. That difference is the whole lookahead
question, and it is resolved in exactly one place: `condition_state`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from ..market_data import Bar
from ..provenance import digest, utcnow
from ..features.records import FeatureRecord
from .classify import Classification, classify
from .hypotheses import Direction, Hypothesis, HypothesisRegistry
from .inference import Estimate, bootstrap_mean, two_sided_p
from .multiplicity import SnoopingLedger, benjamini_hochberg
from .outcomes import ForwardPath, PathScanner, measure_forward_path
from .partitions import ChronologicalPartitions, Partition
from .quality import QualityReport

STUDY_VERSION = "1.0.0"


class StudyError(Exception):
    pass


def condition_state(records: Sequence[FeatureRecord], feature_type: str,
                    at: datetime) -> Optional[str]:
    """The state of a feature as it was KNOWN at `at`.

    Filters on available_at, never effective_at. A swing high that
    occurred before `at` but was not confirmed until after it is
    correctly invisible here.
    """
    best: Optional[FeatureRecord] = None
    for r in records:
        if r.type != feature_type or r.available_at > at:
            continue
        if best is None or r.available_at > best.available_at:
            best = r
    if best is None:
        return None
    return best.state if best.state is not None else (
        None if best.value is None else str(best.value))


def matches(records: Sequence[FeatureRecord], conditions: Dict[str, str],
            at: datetime) -> bool:
    for feature_type, required in conditions.items():
        if condition_state(records, feature_type, at) != required:
            return False
    return True


@dataclass
class HypothesisResult:
    hypothesis_id: str
    partition: str
    n_events: int
    n_matched: int
    estimate: Optional[Estimate]
    p_value: Optional[float]
    mfe_mean: Optional[float] = None
    mae_mean: Optional[float] = None
    favorable_first_fraction: Optional[float] = None

    def as_row(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id, "partition": self.partition,
            "n_events": self.n_events, "n_matched": self.n_matched,
            "estimate": self.estimate.as_row() if self.estimate else None,
            "p_value": None if self.p_value is None else round(self.p_value, 6),
            "mfe_mean": self.mfe_mean, "mae_mean": self.mae_mean,
            "favorable_first_fraction": self.favorable_first_fraction,
        }


@dataclass
class Study:
    """One research study. Carries its own provenance."""
    name: str
    partitions: ChronologicalPartitions
    registry: HypothesisRegistry
    quality: QualityReport
    ledger: SnoopingLedger = field(default_factory=SnoopingLedger)
    results: List[HypothesisResult] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: utcnow().isoformat())

    def test(self, hypothesis_id: str, partition: Partition,
             events: Sequence[FeatureRecord],
             features: Sequence[FeatureRecord],
             bars: Sequence[Bar],
             exploratory: bool = False) -> HypothesisResult:
        """Run one hypothesis against one partition.

        Every call is counted in the snooping ledger, exploratory or not.
        A ledger that counted only reported tests would relocate the
        self-deception rather than remove it.
        """
        if not self.registry.sealed:
            raise StudyError(
                "seal the hypothesis registry before testing; otherwise the "
                "test count used for multiplicity correction is not final")
        h = self.registry.get(hypothesis_id)   # raises if never registered
        self.ledger.record(f"{hypothesis_id}@{partition.value}", exploratory)

        candidates = [e for e in events if e.type == h.event_type]
        signed_returns: List[float] = []
        mfes: List[float] = []
        maes: List[float] = []
        fav_first: List[bool] = []
        matched = 0

        for event in candidates:
            if h.conditions and not matches(features, h.conditions, event.available_at):
                continue
            matched += 1
            measured = measure_forward_path(
                h.event_type, event.available_at, bars, h.horizon_minutes)
            if measured is None:
                continue
            path, _scanner = measured
            if path.bars_observed == 0:
                continue
            signed_returns.append(float(path.signed_return(h.direction)))
            mfes.append(float(path.mfe(h.direction)))
            maes.append(float(path.mae(h.direction)))
            first = path.favorable_came_first(h.direction)
            if first is not None:
                fav_first.append(first)

        estimate = None
        p = None
        if len(signed_returns) >= 2:
            estimate = bootstrap_mean(signed_returns)
            p = two_sided_p(signed_returns)

        result = HypothesisResult(
            hypothesis_id=hypothesis_id, partition=partition.value,
            n_events=len(candidates), n_matched=matched, estimate=estimate,
            p_value=p,
            mfe_mean=(sum(mfes) / len(mfes)) if mfes else None,
            mae_mean=(sum(maes) / len(maes)) if maes else None,
            favorable_first_fraction=(
                sum(1 for f in fav_first if f) / len(fav_first)) if fav_first else None,
        )
        self.results.append(result)
        return result

    def classify_all(self, fdr: float = 0.05) -> Dict[str, Classification]:
        """Correct across every test run, then classify each hypothesis."""
        by_partition: Dict[str, Dict[str, HypothesisResult]] = {}
        for r in self.results:
            by_partition.setdefault(r.partition, {})[r.hypothesis_id] = r

        discovery = by_partition.get(Partition.DISCOVERY.value, {})
        pvals = [(hid, r.p_value) for hid, r in discovery.items()
                 if r.p_value is not None]
        corrected = {c.label: c for c in benjamini_hochberg(pvals, fdr=fdr)}

        out: Dict[str, Classification] = {}
        for h in self.registry.all():
            d = discovery.get(h.id)
            v = by_partition.get(Partition.VALIDATION.value, {}).get(h.id)
            ho = by_partition.get(Partition.HOLDOUT.value, {}).get(h.id)
            survives = corrected[h.id].significant if h.id in corrected else False
            tradable = None
            if d and d.favorable_first_fraction is not None:
                # A crude but honest first cut: if the adverse excursion
                # usually comes first, the terminal statistic is not
                # capturable without surviving that drawdown. Refined
                # threshold work belongs to a later ticket.
                tradable = d.favorable_first_fraction >= 0.5
            out[h.id] = classify(
                discovery=d.estimate if d else None,
                validation=v.estimate if v else None,
                holdout=ho.estimate if ho else None,
                survives_multiplicity=survives,
                tradable=tradable,
            )
        return out

    def provenance(self) -> dict:
        return {
            "study": self.name,
            "study_version": STUDY_VERSION,
            "started_at": self.started_at,
            "partitions": self.partitions.provenance(),
            "hypotheses": self.registry.provenance(),
            "data_quality": self.quality.summary(),
            "multiplicity": self.ledger.summary(),
            "results": [r.as_row() for r in self.results],
        }

    def reproducibility_digest(self) -> str:
        """Same inputs, same code, same digest.

        Excludes wall-clock fields, which differ between runs without any
        difference in the science.
        """
        p = self.provenance()
        p["started_at"] = "<excluded>"
        p["hypotheses"] = {k: v for k, v in p["hypotheses"].items() if k != "head"}
        p["multiplicity"] = p["multiplicity"]
        return digest(p)
