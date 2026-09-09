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

ELIGIBILITY IS DECIDED BEFORE CONDITIONING, NOT AFTER. An observation
that compares two different futures contracts is not a weak
observation to be down-weighted; it is not an observation of the
claim at all, so it is removed before anything is measured. See
`research.eligibility`.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..market_data import Bar
from ..provenance import digest, utcnow
from ..features.contracts import ContractTimeline
from ..features.records import FeatureRecord
from .classify import Classification, classify
from .eligibility import EligibilityReport, filter_eligible
from .identity import ResearchIdentity, registry_structure_digest
from .hypotheses import Direction, Hypothesis, HypothesisRegistry
from .inference import Estimate, bootstrap_mean, two_sided_p
from .multiplicity import SnoopingLedger, benjamini_hochberg
from .outcomes import (BarWindowIndex, ForwardPath, PathScanner,
                       measure_forward_path)
from .partitions import ChronologicalPartitions, Partition
from .quality import QualityReport

STUDY_VERSION = "1.0.0"


class StudyError(Exception):
    pass


def _resolved_state(record: FeatureRecord) -> Optional[str]:
    """The single definition of what a record's state IS.

    Shared by the scanning and the indexed lookup so the two cannot
    disagree about a record they both selected.
    """
    return record.state if record.state is not None else (
        None if record.value is None else str(record.value))


def required_condition_types(registry: HypothesisRegistry) -> Set[str]:
    """Every feature type any registered hypothesis conditions on.

    Derived from the registry, never listed. Conditioning is the ONLY
    thing the study does with feature records, so a record whose type is
    not in this set can never be examined -- which is what makes it safe
    for a caller to keep just these and drop the rest. The frozen set
    conditions on one feature; the engine emits about seventeen per bar,
    so this is the difference between ~2M records and ~30M.
    """
    out: Set[str] = set()
    for h in registry.all():
        out.update(h.conditions)
    return out


class FeatureStateIndex:
    """Condition states by type, in arrival order, for lookup by bisection.

    Scanning every feature record per event is O(events x features) and
    is the second of the two quadratic terms that made the real run
    unfinishable.

    BUILT INCREMENTALLY AND NARROWLY. `add` keeps a record only when its
    type is one the registry actually conditions on, and keeps only the
    two things a lookup reads -- the availability time and the resolved
    state. The FeatureRecord itself is not retained, so the engine's
    output can be consumed as a stream instead of accumulated.

    `usable` is CHECKED, not assumed: availability times must be
    non-decreasing within a type for bisection to be equivalent to the
    scan. They are, because the engine processes bars in order and
    stamps availability at bar close -- but a caller feeding records out
    of order gets the scan, not a wrong answer.
    """

    __slots__ = ("_wanted", "_times", "_states", "usable")

    def __init__(self, wanted: Iterable[str]):
        self._wanted = frozenset(wanted)
        self._times: Dict[str, List[datetime]] = {}
        self._states: Dict[str, List[Optional[str]]] = {}
        self.usable = True

    def add(self, record: FeatureRecord) -> bool:
        """Returns whether the record was kept, for reconciliation."""
        if record.type not in self._wanted:
            return False
        times = self._times.setdefault(record.type, [])
        if times and record.available_at < times[-1]:
            # Out of order: bisection would no longer match the scan.
            self.usable = False
        times.append(record.available_at)
        self._states.setdefault(record.type, []).append(_resolved_state(record))
        return True

    def extend(self, records: Iterable[FeatureRecord]) -> int:
        return sum(1 for r in records if self.add(r))

    def state_at(self, feature_type: str, at: datetime) -> Optional[str]:
        times = self._times.get(feature_type)
        if not times:
            return None
        i = bisect_right(times, at) - 1
        if i < 0:
            return None
        # The scan keeps the FIRST record among equal availability times
        # (its comparison is strictly greater-than), so walk back over
        # any tie to select the same one. Ties cannot occur for a
        # per-bar feature, but matching the scan exactly costs nothing
        # and removes the need to argue that they cannot.
        while i > 0 and times[i - 1] == times[i]:
            i -= 1
        return self._states[feature_type][i]

    def summary(self) -> dict:
        return {"types": sorted(self._times),
                "records": sum(len(v) for v in self._times.values()),
                "usable": self.usable}


def condition_state(records: Sequence[FeatureRecord], feature_type: str,
                    at: datetime,
                    index: Optional[FeatureStateIndex] = None) -> Optional[str]:
    """The state of a feature as it was KNOWN at `at`.

    Filters on available_at, never effective_at. A swing high that
    occurred before `at` but was not confirmed until after it is
    correctly invisible here.

    `index` changes only how the record is found; the answer is the same.
    """
    if index is not None and index.usable:
        return index.state_at(feature_type, at)
    best: Optional[FeatureRecord] = None
    for r in records:
        if r.type != feature_type or r.available_at > at:
            continue
        if best is None or r.available_at > best.available_at:
            best = r
    if best is None:
        return None
    return _resolved_state(best)


def matches(records: Sequence[FeatureRecord], conditions: Dict[str, str],
            at: datetime,
            index: Optional[FeatureStateIndex] = None) -> bool:
    for feature_type, required in conditions.items():
        if condition_state(records, feature_type, at, index) != required:
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
    # n_events counts events that SURVIVED eligibility. The refused ones
    # are reported here rather than merely subtracted, so a shrunken
    # sample always carries the reason it shrank.
    eligibility: Optional[EligibilityReport] = None

    def as_row(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id, "partition": self.partition,
            "n_events": self.n_events, "n_matched": self.n_matched,
            "estimate": self.estimate.as_row() if self.estimate else None,
            "p_value": None if self.p_value is None else round(self.p_value, 6),
            "mfe_mean": self.mfe_mean, "mae_mean": self.mae_mean,
            "favorable_first_fraction": self.favorable_first_fraction,
            "eligibility": (self.eligibility.as_row()
                            if self.eligibility else None),
        }


@dataclass
class Study:
    """One research study. Carries its own provenance."""
    name: str
    partitions: ChronologicalPartitions
    registry: HypothesisRegistry
    quality: QualityReport
    # The contract provenance of every session in the sample. Optional
    # in the type only: a study that omits it can establish no
    # prior-session comparison as within-contract, so every such
    # hypothesis reports zero eligible events and says why.
    contracts: Optional[ContractTimeline] = None
    # Build the bar index when a caller does not supply one. Set False
    # to force the scanning path. That exists so the unoptimized code is
    # REACHABLE: a benchmark whose reference arm quietly took the fast
    # path would report a speedup of 1.1x, and an equivalence test
    # written the same way would compare the fast path against itself.
    # Both happened before this flag did.
    accelerate: bool = True
    # The design's semantic identity, when the caller has built one.
    # Reported alongside the results so a report states which research
    # design produced it, in a form another process can compare against.
    identity: Optional[ResearchIdentity] = None
    ledger: SnoopingLedger = field(default_factory=SnoopingLedger)
    results: List[HypothesisResult] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: utcnow().isoformat())

    def test(self, hypothesis_id: str, partition: Partition,
             events: Sequence[FeatureRecord],
             features: Sequence[FeatureRecord],
             bars: Sequence[Bar],
             exploratory: bool = False,
             bar_index: Optional[BarWindowIndex] = None,
             condition_index: Optional[FeatureStateIndex] = None
             ) -> HypothesisResult:
        """Run one hypothesis against one partition.

        Every call is counted in the snooping ledger, exploratory or not.
        A ledger that counted only reported tests would relocate the
        self-deception rather than remove it.

        The two indexes are pure accelerators: omit them and the same
        numbers come out of the same code, located by scanning. A caller
        that passes `condition_index` may pass `features=()` with it,
        because the index already holds every record conditioning could
        have read.
        """
        if not self.registry.sealed:
            raise StudyError(
                "seal the hypothesis registry before testing; otherwise the "
                "test count used for multiplicity correction is not final")
        h = self.registry.get(hypothesis_id)   # raises if never registered
        self.ledger.record(f"{hypothesis_id}@{partition.value}", exploratory)

        candidates = [e for e in events if e.type == h.event_type]
        # The contract-boundary rule, applied before any measurement.
        # Scoped to this hypothesis: a session refused here is still in
        # the sample for every hypothesis that does not reach back
        # across its boundary.
        candidates, eligibility = filter_eligible(h, candidates, self.contracts)
        signed_returns: List[float] = []
        mfes: List[float] = []
        maes: List[float] = []
        fav_first: List[bool] = []
        matched = 0

        if bar_index is None and self.accelerate:
            bar_index = self._bar_index_for(bars)
        for event in candidates:
            if h.conditions and not matches(features, h.conditions,
                                            event.available_at, condition_index):
                continue
            matched += 1
            measured = measure_forward_path(
                h.event_type, event.available_at, bars, h.horizon_minutes,
                bar_index)
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
            eligibility=eligibility,
        )
        self.results.append(result)
        return result

    def _bar_index_for(self, bars: Sequence[Bar]) -> BarWindowIndex:
        """One index per bar sequence, reused across the twelve tests.

        The memo holds the sequence itself, not just its id, so the
        object cannot be collected and its id handed to a different
        sequence while the memo still claims to describe it.
        """
        cached = getattr(self, "_bar_index_memo", None)
        if cached is not None and cached.bars is bars:
            return cached
        index = BarWindowIndex(bars)
        self._bar_index_memo = index
        return index

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
            "research_identity": (self.identity.as_row()
                                  if self.identity else None),
            "contract_provenance": (self.contracts.summary()
                                    if self.contracts else None),
            "multiplicity": self.ledger.summary(),
            "results": [r.as_row() for r in self.results],
        }

    def reproducibility_digest(self) -> str:
        """Same inputs, same code, same digest -- ACROSS PROCESSES.

        It did not used to be. `provenance()["hypotheses"]` carries each
        entry's chain hash, and those are computed over `registered_at`,
        which comes from the wall clock when `preregistered.py` is
        imported. Two processes running byte-identical code on
        byte-identical data therefore produced different digests, so the
        one comparison this exists to support -- "is this the same study
        I ran before?" -- always answered no.

        The chain is not the thing to change: it is the audit record
        proving no hypothesis was inserted or edited after registration,
        and rewriting it to drop the timestamps would destroy that
        evidence. So the chain is reported in full by `provenance()` and
        summarised here by its STRUCTURE instead: what the hypotheses
        say and in what order, which changes if and only if the frozen
        set changes.

        Wall-clock fields are excluded for the same reason they always
        were: they differ between runs without any difference in the
        science.
        """
        p = self.provenance()
        p["started_at"] = "<excluded>"
        hypotheses = p["hypotheses"]
        p["hypotheses"] = {
            "count": hypotheses.get("count"),
            "sealed": hypotheses.get("sealed"),
            "chain_valid": hypotheses.get("chain_valid"),
            # Structure, not the timestamp-bearing chain hashes.
            "structure": registry_structure_digest(self.registry),
        }
        return digest(p)
