"""The provenance contract.

WHAT THIS MUST ANSWER, years from now, without relying on memory or
undocumented code state:

    "Did ORB v4 outperform ORB v3 in this regime?"

That decomposes into four questions, and the contract exists to make each
one answerable from the database alone:

    which implementation produced this?   -> component_versions
    under which configuration?            -> component_versions.config_digest
    from which inputs?                    -> artifact_edges
    describing which moment in time?      -> observed_at / captured_at / generated_at

DELIBERATELY MINIMAL. This is not a provenance framework. It is five
tables and one rule, and it stops there. Future stages own their domain
columns; they attach to this spine rather than reinventing it.

THE THREE TIMESTAMPS are the part most likely to be eroded by a hurried
future change, so they are stated plainly:

    observed_at   when the fact was true in the market
    captured_at   when we received it
    generated_at  when this artifact was computed

A feature computed at 09:31 from a bar that closed at 09:30 and reached us
at 09:30:04 has three different times. Collapsing them makes a stale input
indistinguishable from a live one -- precisely the error a deterministic
feature engine must never make, and the reason the retired system's
separation of observed_at from captured_at was worth carrying over.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# The North Star pipeline, named. Naming the stages is not building them:
# no stage logic exists in this repository, and T-001 does not add any.
STAGES = (
    "market_observation",
    "feature",
    "market_state",
    "research",
    "setup_candidate",
    "entry_candidate",
    "ai_analysis",
    "adversarial_review",
    "delivery",
)

# Which capability identity may author which stage. This mirrors the
# database's own `stage_scoped_artifact_write` policy exactly; the database
# is the enforcement point, this is here so application code can fail early
# and legibly rather than on a permission error.
#
# Note analyst and reviewer: separate identities, separate stages, in both
# places. An adversarial review whose author can write the thing it
# reviews is not adversarial.
STAGE_WRITERS = {
    "market_observation": "md_ingest",
    "feature": "feature_engine",
    "market_state": "feature_engine",
    "research": "setup_detector",
    "setup_candidate": "setup_detector",
    "entry_candidate": "setup_detector",
    "ai_analysis": "analyst",
    "adversarial_review": "reviewer",
    "delivery": "delivery",
}


def digest(payload: Any) -> str:
    """A stable content digest. Sorted keys and separators so the same
    logical configuration hashes identically across processes and Python
    versions -- otherwise 'same config' comparisons silently fail and the
    ORB v3/v4 question becomes unanswerable."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    """Timezone-aware UTC. Never datetime.utcnow(), which returns a naive
    value that compares wrongly against the timestamptz columns this
    system stores."""
    return datetime.now(timezone.utc)


class ProvenanceError(Exception):
    """Raised when a record would be unreconstructable. Provenance that
    can be skipped is not provenance, so these are hard errors rather
    than warnings."""


@dataclass(frozen=True)
class ComponentVersion:
    """Which implementation, under which configuration."""

    component: str
    version: str
    config_digest: str
    source_ref: Optional[str] = None
    description: Optional[str] = None

    @staticmethod
    def of(component: str, version: str, config: Any, *,
           source_ref: Optional[str] = None,
           description: Optional[str] = None) -> "ComponentVersion":
        if not component or not version:
            raise ProvenanceError("component and version are both required")
        return ComponentVersion(
            component=component, version=version, config_digest=digest(config),
            source_ref=source_ref, description=description,
        )


@dataclass(frozen=True)
class ModelVersion:
    """Which model, and which instruction set. Kept separate from
    ComponentVersion because an AI stage has BOTH -- its harness/parsing/
    guardrails, and the model behind them. Collapsing them makes "same
    prompt, new model" indistinguishable from "new prompt, same model",
    which is exactly the comparison an adversarial review pipeline needs
    to be able to make."""

    provider: str
    model: str
    prompt_version: str
    parameters_digest: Optional[str] = None

    @staticmethod
    def of(provider: str, model: str, prompt_version: str,
           parameters: Any = None) -> "ModelVersion":
        if not (provider and model and prompt_version):
            raise ProvenanceError("provider, model and prompt_version are all required")
        return ModelVersion(
            provider=provider, model=model, prompt_version=prompt_version,
            parameters_digest=digest(parameters) if parameters is not None else None,
        )


@dataclass
class ArtifactRecord:
    """One node in a provenance chain.

    `parents` are the artifact ids this was derived from. Empty is valid
    and meaningful for a market_observation -- raw evidence has no
    upstream artifact -- and invalid for anything downstream, which is
    what `validate` enforces: a derived artifact with no inputs cannot be
    reconstructed, so it is a hard error rather than a silent gap.
    """

    stage: str
    run_id: str
    parents: list = field(default_factory=list)
    source_ref: Optional[str] = None
    observed_at: Optional[datetime] = None
    captured_at: Optional[datetime] = None
    generated_at: Optional[datetime] = None
    payload: Any = None

    def __post_init__(self):
        if self.generated_at is None:
            self.generated_at = utcnow()

    @property
    def payload_digest(self) -> Optional[str]:
        return None if self.payload is None else digest(self.payload)

    def validate(self) -> "ArtifactRecord":
        if self.stage not in STAGES:
            raise ProvenanceError(f"unknown stage: {self.stage!r}")
        if not self.run_id:
            raise ProvenanceError("run_id is required -- an artifact with no run cannot be traced")
        if self.stage == "market_observation":
            # Raw evidence: no upstream artifact, but it MUST say when the
            # fact was true and when we received it, or downstream staleness
            # checks have nothing to work from.
            if self.observed_at is None or self.captured_at is None:
                raise ProvenanceError(
                    "a market_observation requires both observed_at and captured_at"
                )
        else:
            if not self.parents:
                raise ProvenanceError(
                    f"stage {self.stage!r} is derived and requires at least one parent artifact"
                )
        for ts in (self.observed_at, self.captured_at, self.generated_at):
            if ts is not None and ts.tzinfo is None:
                raise ProvenanceError("timestamps must be timezone-aware")
        return self


def writer_for(stage: str) -> str:
    """Which capability identity may author this stage."""
    try:
        return STAGE_WRITERS[stage]
    except KeyError:
        raise ProvenanceError(f"unknown stage: {stage!r}") from None
