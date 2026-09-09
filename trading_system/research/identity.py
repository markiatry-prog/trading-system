"""The semantic identity of a research design.

WHAT PROBLEM THIS SOLVES. `Study.reproducibility_digest` promised that
the same inputs and the same code produce the same digest. It did not:
each Hypothesis stamps `registered_at` from the wall clock when
`preregistered.py` is imported, so the chain hashes differ between two
processes running byte-identical code on byte-identical data. Any
cross-run comparison -- "is this the same study I ran last week?" --
silently answered no, which is the failure mode a reproducibility
digest exists to prevent.

WHY THE CHAIN IS NOT THE FIX. The hash chain is an AUDIT record. It
proves that no hypothesis was inserted, removed or edited within the
history it covers, and rewriting it to remove the timestamps would
destroy exactly the evidence it exists to provide. So the chain stays
untouched and this module adds a second, orthogonal digest beside it:

  the CHAIN answers   "was this set tampered with after registration?"
  the IDENTITY answers "is this the same research design?"

WHAT COUNTS AS MEANING. Everything that could change a number in the
report, and nothing that could not:

  hypotheses        id, statement, event type, direction, horizon,
                    conditions, falsifier -- and their ORDER, because
                    the multiplicity correction depends on the set
  outcome spec      how a forward path is measured and estimated:
                    bootstrap parameters and seed, the power floor,
                    the FDR level
  eligibility       which primitives reach across a session boundary,
                    and the reason recorded when one is refused
  quality gate      its thresholds AND the days it actually passed
  partitions        the exact day range of each stage
  versions          engine, study, feature config
  dataset           sha256, byte count, acquisition request digest
  code              a digest over every module that computes a number

WHAT IS DELIBERATELY EXCLUDED. `registered_at`, `started_at`, ledger
timestamps, checkpoint timestamps, file paths, the run id. None of them
can change a result; all of them change between runs.

DETERMINISM IS A PROPERTY OF THIS FILE. Every set is sorted before it
is hashed, every enum contributes its `value`, and nothing hashes a
Python object identity or relies on `hash()`, which is randomised per
process. `tests/test_identity.py` proves it by computing the digest in
two separate interpreters.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ..features.contracts import (EXCLUSION_REASON, PRIOR_SESSION_EVENTS,
                                  PRIOR_SESSION_FEATURES)
from ..provenance import digest
from .hypotheses import HypothesisRegistry

SEMANTIC_DIGEST_VERSION = "1.0.0"


class IdentityError(Exception):
    """A digest that cannot be computed honestly is not computed."""


# Every module whose source can change a number in the report.
COMPUTING_MODULES = (
    "features/calendar.py", "features/config.py", "features/contracts.py",
    "features/engine.py", "features/records.py", "features/roll.py",
    "market_data.py",
    "research/classify.py", "research/eligibility.py",
    "research/hypotheses.py", "research/identity.py",
    "research/inference.py", "research/multiplicity.py",
    "research/outcomes.py", "research/partitions.py",
    "research/preregistered.py", "research/quality.py", "research/study.py",
    "sources/databento_source.py",
)


def code_digest(package_root: Optional[Path] = None) -> str:
    """A digest over the source of everything that computes a number."""
    root = package_root or Path(__file__).resolve().parents[1]
    parts = []
    for rel in COMPUTING_MODULES:
        path = root / rel
        if not path.exists():
            raise IdentityError(
                f"{rel} is missing; refusing to compute a code digest that "
                f"would silently exclude it")
        parts.append([rel, hashlib.sha256(path.read_bytes()).hexdigest()])
    return digest(parts)


def registry_structure_digest(registry: HypothesisRegistry) -> str:
    """What the hypotheses SAY, in order. Independent of the wall clock.

    Order is part of it: benjamini_hochberg ranks p-values across the
    set actually registered, so a reordered or resized set is a
    different multiple-comparison correction and therefore a different
    study.
    """
    body = [
        [h.id, h.statement, h.event_type, h.direction.value,
         h.horizon_minutes, sorted(h.conditions.items()), h.falsifier]
        for h in registry.all()
    ]
    return digest(body)


def outcome_spec_digest(bootstrap_confidence: float = 0.95,
                        bootstrap_iterations: int = 2000,
                        bootstrap_seed: int = 20260907,
                        fdr: float = 0.05) -> str:
    """How a measurement becomes a verdict.

    The bootstrap seed is in here because a different seed is a
    different interval, and an interval decides whether a hypothesis is
    REJECTED. The power floor is in here because it decides whether an
    absence of evidence is reported as evidence of absence.
    """
    from .inference import MIN_SAMPLES_FOR_ANY_CLAIM, MIN_SAMPLES_FOR_SUBGROUP
    return digest({
        "bootstrap_confidence": bootstrap_confidence,
        "bootstrap_iterations": bootstrap_iterations,
        "bootstrap_seed": bootstrap_seed,
        "fdr": fdr,
        "min_samples_for_any_claim": MIN_SAMPLES_FOR_ANY_CLAIM,
        "min_samples_for_subgroup": MIN_SAMPLES_FOR_SUBGROUP,
        # Excursions are signed in points from the reference close, and
        # ordering is decided by first-touch. Naming it here means a
        # change of convention shows up as a different design even
        # though the code digest would also catch it.
        "excursion_units": "points_from_reference_close",
        "ordering_rule": "first_strict_extreme",
    })


def eligibility_digest() -> str:
    """Which observations are admitted, and under what name they are not.

    Sorted, because these are frozensets and set iteration order is not
    guaranteed to be stable across processes.
    """
    return digest({
        "exclusion_reason": EXCLUSION_REASON,
        "prior_session_events": sorted(PRIOR_SESSION_EVENTS),
        "prior_session_features": sorted(PRIOR_SESSION_FEATURES),
    })


def quality_digest(thresholds, passed_days: Sequence[date]) -> str:
    """The gate's settings AND its verdict.

    Both, deliberately. A threshold change that happens not to move any
    day is harmless; a day list that moved without a threshold change
    means the data or the calendar did, and that is not.
    """
    fields = {k: str(v) for k, v in sorted(asdict(thresholds).items())}
    return digest([fields, [d.isoformat() for d in sorted(passed_days)]])


def partition_digest(partitions) -> str:
    """The stage boundaries. Unseal records are excluded: opening the
    holdout is an event in a run, not a property of the design."""
    prov = partitions.provenance()
    return digest({k: v for k, v in sorted(prov.items())
                   if k not in ("unseals", "holdout_ever_unsealed")})


def dataset_digest(sha256: str, byte_count: int, request_digest: str) -> str:
    return digest({"sha256": sha256, "bytes": int(byte_count),
                   "request_digest": request_digest})


@dataclass(frozen=True)
class ResearchIdentity:
    """One digest for one research design. Stable across processes."""
    semantic_version: str
    study_version: str
    engine_version: str
    symbol: str
    feature_config: str
    feature_config_name: str
    hypotheses: str
    outcome_spec: str
    eligibility: str
    quality_gate: str
    partitions: str
    dataset: str
    code: str

    @classmethod
    def build(cls, *, study_version: str, engine_version: str, symbol: str,
              feature_config, registry: HypothesisRegistry,
              thresholds, passed_days: Sequence[date], partitions,
              dataset_sha256: str, dataset_bytes: int,
              request_digest: str,
              package_root: Optional[Path] = None) -> "ResearchIdentity":
        return cls(
            semantic_version=SEMANTIC_DIGEST_VERSION,
            study_version=study_version,
            engine_version=engine_version,
            symbol=symbol,
            feature_config=feature_config.digest(),
            feature_config_name=feature_config.name,
            hypotheses=registry_structure_digest(registry),
            outcome_spec=outcome_spec_digest(),
            eligibility=eligibility_digest(),
            quality_gate=quality_digest(thresholds, passed_days),
            partitions=partition_digest(partitions),
            dataset=dataset_digest(dataset_sha256, dataset_bytes,
                                   request_digest),
            code=code_digest(package_root),
        )

    def digest(self) -> str:
        return digest(dict(sorted(asdict(self).items())))

    def differences(self, other: "ResearchIdentity") -> list:
        mine, theirs = asdict(self), asdict(other)
        return [k for k in sorted(mine) if mine[k] != theirs[k]]

    def as_row(self) -> Dict[str, Any]:
        row = dict(sorted(asdict(self).items()))
        row["semantic_digest"] = self.digest()
        return row
