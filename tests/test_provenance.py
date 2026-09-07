"""The provenance contract must make an artifact reconstructable, or
refuse to produce it."""
import pytest
from datetime import datetime, timezone
from trading_system.provenance import (
    STAGES, ArtifactRecord, ComponentVersion, ModelVersion, ProvenanceError,
    digest, utcnow, writer_for,
)

T = datetime(2026, 9, 6, 13, 30, tzinfo=timezone.utc)


def test_digest_is_stable_and_order_independent():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})


def test_component_version_distinguishes_config():
    v3 = ComponentVersion.of("orb", "v3", {"window": 5})
    v4 = ComponentVersion.of("orb", "v4", {"window": 5})
    v3b = ComponentVersion.of("orb", "v3", {"window": 15})
    assert v3.version != v4.version
    # The ORB v3-vs-v4 question needs config changes to be visible too.
    assert v3.config_digest != v3b.config_digest


def test_component_version_requires_identity():
    with pytest.raises(ProvenanceError):
        ComponentVersion.of("", "v1", {})
    with pytest.raises(ProvenanceError):
        ComponentVersion.of("orb", "", {})


def test_model_version_separates_model_from_prompt():
    a = ModelVersion.of("prov", "model-a", "p1")
    b = ModelVersion.of("prov", "model-b", "p1")
    c = ModelVersion.of("prov", "model-a", "p2")
    assert a != b and a != c          # same prompt/new model != new prompt/same model
    with pytest.raises(ProvenanceError):
        ModelVersion.of("prov", "model-a", "")


def test_market_observation_requires_both_observation_and_capture_times():
    ok = ArtifactRecord(stage="market_observation", run_id="r1",
                        observed_at=T, captured_at=T).validate()
    assert ok.generated_at is not None
    with pytest.raises(ProvenanceError):
        ArtifactRecord(stage="market_observation", run_id="r1", observed_at=T).validate()
    with pytest.raises(ProvenanceError):
        ArtifactRecord(stage="market_observation", run_id="r1", captured_at=T).validate()


@pytest.mark.parametrize("stage", [s for s in STAGES if s != "market_observation"])
def test_derived_artifacts_require_a_parent(stage):
    with pytest.raises(ProvenanceError):
        ArtifactRecord(stage=stage, run_id="r1").validate()
    ArtifactRecord(stage=stage, run_id="r1", parents=["a1"]).validate()


def test_run_id_is_mandatory():
    with pytest.raises(ProvenanceError):
        ArtifactRecord(stage="feature", run_id="", parents=["a1"]).validate()


def test_unknown_stage_is_rejected():
    with pytest.raises(ProvenanceError):
        ArtifactRecord(stage="guesswork", run_id="r1", parents=["a"]).validate()
    with pytest.raises(ProvenanceError):
        writer_for("guesswork")


def test_naive_timestamps_are_rejected():
    with pytest.raises(ProvenanceError):
        ArtifactRecord(stage="market_observation", run_id="r1",
                       observed_at=datetime(2026, 9, 6, 13, 30),
                       captured_at=T).validate()


def test_utcnow_is_timezone_aware():
    assert utcnow().tzinfo is not None


def test_the_three_timestamps_are_independent():
    """A feature computed at 09:31 from a bar observed at 09:30 that
    arrived at 09:30:04 is three different times."""
    observed = datetime(2026, 9, 6, 13, 30, 0, tzinfo=timezone.utc)
    captured = datetime(2026, 9, 6, 13, 30, 4, tzinfo=timezone.utc)
    generated = datetime(2026, 9, 6, 13, 31, 0, tzinfo=timezone.utc)
    a = ArtifactRecord(stage="feature", run_id="r1", parents=["obs1"],
                       observed_at=observed, captured_at=captured,
                       generated_at=generated).validate()
    assert a.observed_at < a.captured_at < a.generated_at


def test_analyst_and_reviewer_are_different_writers():
    """The separation the whole adversarial design rests on."""
    assert writer_for("ai_analysis") == "analyst"
    assert writer_for("adversarial_review") == "reviewer"
    assert writer_for("ai_analysis") != writer_for("adversarial_review")


def test_every_stage_has_exactly_one_declared_writer():
    for stage in STAGES:
        assert writer_for(stage)


def test_payload_digest_is_present_only_when_there_is_a_payload():
    assert ArtifactRecord(stage="feature", run_id="r", parents=["p"]).payload_digest is None
    assert ArtifactRecord(stage="feature", run_id="r", parents=["p"],
                          payload={"x": 1}).payload_digest is not None
