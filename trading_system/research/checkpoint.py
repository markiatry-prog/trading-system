"""Resumable study state, bound to everything that determines the output.

WHY THIS EXISTS. The real NQ study runs for many minutes over 1.7M
bars, and a desktop that reboots partway through should not force it to
start from zero. It has already cost two runs.

WHY IT IS DANGEROUS, AND WHAT MAKES IT SAFE. A checkpoint is a claim
that some work does not need redoing. If anything that would have
changed the answer has changed since the checkpoint was written, that
claim is false and resuming produces a result that never corresponded
to any single version of the code or the data -- while looking
completely ordinary. So the checkpoint carries a KEY digesting every
input that determines the output, and a mismatch is refused rather than
reconciled:

That key is the study's SEMANTIC IDENTITY, defined once in
`research.identity` and shared with the report so both answer "is this
the same research design?" the same way: the dataset, the symbol, the
engine and config versions, the hypotheses' structure, the outcome
specification, the eligibility rules, the quality gate's thresholds and
verdict, the partitions, and a digest over every module that computes a
number.

It is stable across processes, which the wall-clock-stamped hypothesis
chain is not -- see `research.identity` for why the chain stays as it is
and this sits beside it rather than replacing it.

THE HOLDOUT IS NEVER IN A CHECKPOINT. Not as a policy but as a
mechanism: saving a holdout stage raises, and loading a file that
contains one refuses the whole checkpoint. A sealed partition that
could be resumed from disk is not sealed.

ATOMICITY. Written to a temporary file, fsynced, then renamed. A crash
leaves the previous complete checkpoint or the new complete one, never
a torn one. A `body_digest` over the contents catches anything that
edits the file afterwards, torn writes included.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..provenance import digest, utcnow
from .eligibility import EligibilityReport
from .identity import ResearchIdentity
from .inference import Estimate
from .multiplicity import SnoopingLedger
from .partitions import Partition
from .study import HypothesisResult, Study

CHECKPOINT_VERSION = "1.0.0"


class CheckpointError(Exception):
    """A checkpoint that cannot be trusted. Always fatal, never repaired."""


@dataclass(frozen=True)
class CheckpointKey:
    """The format version, plus the research design's semantic identity.

    Everything that determines the OUTPUT lives in `identity`, which is
    stable across processes by construction. `checkpoint_version` is the
    one thing here that is about the file rather than the science: a
    format change invalidates the file without the design having moved.
    """
    checkpoint_version: str
    identity: ResearchIdentity

    @classmethod
    def for_identity(cls, identity: ResearchIdentity) -> "CheckpointKey":
        return cls(checkpoint_version=CHECKPOINT_VERSION, identity=identity)

    def digest(self) -> str:
        return digest({"checkpoint_version": self.checkpoint_version,
                       "identity": self.identity.digest()})

    def differences(self, other: "CheckpointKey") -> List[str]:
        out = [] if self.checkpoint_version == other.checkpoint_version \
            else ["checkpoint_version"]
        return out + self.identity.differences(other.identity)

    def as_json(self) -> dict:
        return {"checkpoint_version": self.checkpoint_version,
                "identity": self.identity.as_row()}

    @classmethod
    def from_json(cls, data: dict) -> "CheckpointKey":
        fields = {k: v for k, v in data["identity"].items()
                  if k != "semantic_digest"}
        return cls(checkpoint_version=data["checkpoint_version"],
                   identity=ResearchIdentity(**fields))


def _estimate_to_json(e: Optional[Estimate]) -> Optional[dict]:
    """Raw floats, not `as_row`.

    `as_row` rounds to six decimals for reporting. Round-tripping
    through it would let a confidence bound sitting within 5e-7 of zero
    change which side of zero it is on, and `excludes_zero` decides a
    verdict. A resumed run must not be able to differ from an
    uninterrupted one by so much as a bit.
    """
    if e is None:
        return None
    return {"n": e.n, "mean": e.mean, "median": e.median, "stdev": e.stdev,
            "ci_low": e.ci_low, "ci_high": e.ci_high,
            "confidence": e.confidence, "seed": e.seed}


def _estimate_from_json(d: Optional[dict]) -> Optional[Estimate]:
    return None if d is None else Estimate(**d)


def _eligibility_to_json(e: Optional[EligibilityReport]) -> Optional[dict]:
    if e is None:
        return None
    return {"rule": e.rule, "applies": e.applies,
            "events_considered": e.events_considered,
            "events_eligible": e.events_eligible,
            "events_excluded": e.events_excluded,
            "excluded_by_verdict": dict(e.excluded_by_verdict),
            "sessions_excluded": list(e.sessions_excluded)}


def _eligibility_from_json(d: Optional[dict]) -> Optional[EligibilityReport]:
    return None if d is None else EligibilityReport(**d)


def _result_to_json(r: HypothesisResult) -> dict:
    return {"hypothesis_id": r.hypothesis_id, "partition": r.partition,
            "n_events": r.n_events, "n_matched": r.n_matched,
            "estimate": _estimate_to_json(r.estimate), "p_value": r.p_value,
            "mfe_mean": r.mfe_mean, "mae_mean": r.mae_mean,
            "favorable_first_fraction": r.favorable_first_fraction,
            "eligibility": _eligibility_to_json(r.eligibility)}


def _result_from_json(d: dict) -> HypothesisResult:
    return HypothesisResult(
        hypothesis_id=d["hypothesis_id"], partition=d["partition"],
        n_events=d["n_events"], n_matched=d["n_matched"],
        estimate=_estimate_from_json(d["estimate"]), p_value=d["p_value"],
        mfe_mean=d["mfe_mean"], mae_mean=d["mae_mean"],
        favorable_first_fraction=d["favorable_first_fraction"],
        eligibility=_eligibility_from_json(d["eligibility"]))


@dataclass
class StudyCheckpoint:
    """Completed stages and the ledger that recorded them."""
    key: CheckpointKey
    stages: Dict[str, dict] = field(default_factory=dict)
    ledger_entries: List[Dict[str, str]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())

    # -- content -------------------------------------------------------

    def completed(self) -> List[str]:
        return sorted(self.stages)

    def has(self, partition: Partition) -> bool:
        return partition.value in self.stages

    def record_stage(self, partition: Partition,
                     results: Sequence[HypothesisResult],
                     ledger: SnoopingLedger,
                     sessions: int, events: int,
                     rows: Optional[Sequence[dict]] = None) -> None:
        """`rows` carries an analysis's own per-stage output -- T-004B's
        baseline comparisons -- alongside the hypothesis results, so a
        second analysis gets the same atomicity, key checking and
        holdout refusal without a second checkpoint format."""
        if partition is Partition.HOLDOUT:
            raise CheckpointError(
                "refusing to checkpoint the holdout. A sealed partition "
                "whose results can be reloaded from disk is not sealed, and "
                "the seal is the only thing standing between a final test "
                "and an iterated one.")
        self.stages[partition.value] = {
            "results": [_result_to_json(r) for r in results],
            "rows": list(rows) if rows else [],
            "sessions": sessions, "events": events,
            "completed_at": utcnow().isoformat(),
        }
        self.ledger_entries = [dict(e) for e in ledger.entries]
        self.updated_at = utcnow().isoformat()

    def rows_for(self, partition: Partition) -> List[dict]:
        stage = self.stages.get(partition.value)
        return list(stage.get("rows", [])) if stage else []

    def results_for(self, partition: Partition) -> List[HypothesisResult]:
        stage = self.stages.get(partition.value)
        if stage is None:
            raise CheckpointError(f"no {partition.value} stage in this checkpoint")
        return [_result_from_json(d) for d in stage["results"]]

    def restore_into(self, study: Study, partition: Partition) -> int:
        """Replay a completed stage into a fresh Study.

        Results are appended in stored order and the ledger is restored
        wholesale, so a resumed study's `results` list and test count are
        the same objects in the same order an uninterrupted run would
        have produced.
        """
        restored = self.results_for(partition)
        study.results.extend(restored)
        study.ledger.entries = [dict(e) for e in self.ledger_entries]
        return len(restored)

    # -- serialisation -------------------------------------------------

    def _body(self) -> dict:
        return {
            "checkpoint_version": CHECKPOINT_VERSION,
            "key": self.key.as_json(),
            "key_digest": self.key.digest(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stages": self.stages,
            "ledger_entries": self.ledger_entries,
        }

    def save(self, path) -> None:
        """Atomic. A crash leaves the old file or the new one, never both
        halves of each."""
        if Partition.HOLDOUT.value in self.stages:
            raise CheckpointError("holdout results must never be persisted")
        body = self._body()
        payload = json.dumps({**body, "body_digest": digest(body)},
                             indent=1, sort_keys=True)
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".partial")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, expected: CheckpointKey) -> "StudyCheckpoint":
        """Load, or raise. There is no partial acceptance."""
        raw = Path(path).read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CheckpointError(
                f"{path} is not valid JSON ({exc}); refusing to resume from "
                f"a file that may have been truncated") from None

        stored_digest = data.pop("body_digest", None)
        if stored_digest is None:
            raise CheckpointError(f"{path} carries no body_digest")
        if digest(data) != stored_digest:
            raise CheckpointError(
                f"{path} does not match its own digest. It has been edited "
                f"or written incompletely; resuming from it could produce a "
                f"report that never corresponded to any real run.")

        if data.get("checkpoint_version") != CHECKPOINT_VERSION:
            raise CheckpointError(
                f"checkpoint format {data.get('checkpoint_version')} != "
                f"{CHECKPOINT_VERSION}")
        if Partition.HOLDOUT.value in (data.get("stages") or {}):
            raise CheckpointError(
                "this checkpoint contains holdout results, which this code "
                "never writes. Refusing it outright.")

        key = CheckpointKey.from_json(data["key"])
        if key.digest() != data.get("key_digest"):
            raise CheckpointError("the stored key does not match its digest")
        if key != expected:
            changed = expected.differences(key)
            raise CheckpointError(
                "this checkpoint was written under different inputs and "
                "cannot be resumed. Changed: " + ", ".join(changed) +
                ". Delete it and rerun, or restore the previous inputs.")

        return cls(key=key, stages=data["stages"],
                   ledger_entries=data.get("ledger_entries", []),
                   created_at=data["created_at"],
                   updated_at=data["updated_at"])
