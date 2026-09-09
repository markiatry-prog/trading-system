"""The semantic research digest, and the property that makes it useful.

THE BUG THIS CLOSES. `Study.reproducibility_digest` promised "same
inputs, same code, same digest" and did not deliver it. Each Hypothesis
stamps `registered_at` from the wall clock when `preregistered.py` is
imported, so the chain hashes -- which the digest included -- differed
between two processes running byte-identical code on byte-identical
data. The one comparison it existed to support always answered "no".

The chain itself is NOT the thing to change. It is the audit record
proving nothing was inserted or edited after registration; rewriting it
to drop the timestamps would destroy that evidence. So it stays exactly
as it was, reported in full, and a second orthogonal digest sits beside
it.

The decisive tests here run in SEPARATE INTERPRETERS. A digest computed
twice inside one process can be stable for reasons that do not survive a
fresh import -- a cached module, a set that happens to iterate the same
way, a hash seed that has not changed. Only a subprocess proves it.
"""
import json
import subprocess
import sys
import textwrap
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from trading_system.features.config import FeatureConfig
from trading_system.features.engine import ENGINE_VERSION
from trading_system.research.identity import (
    COMPUTING_MODULES, IdentityError, ResearchIdentity,
    SEMANTIC_DIGEST_VERSION, code_digest, eligibility_digest,
    outcome_spec_digest, partition_digest, quality_digest,
    registry_structure_digest)
from trading_system.research.partitions import split_chronologically
from trading_system.research.preregistered import build_registry
from trading_system.research.quality import QualityReport, QualityThresholds

ROOT = Path(__file__).resolve().parents[1]
DAYS = [date(2021, 9, 7) + timedelta(days=i) for i in range(40)]


def build(**overrides) -> ResearchIdentity:
    base = ResearchIdentity.build(
        study_version="1.0.0", engine_version=ENGINE_VERSION,
        symbol="NQ.c.0", feature_config=FeatureConfig(),
        registry=build_registry(), thresholds=QualityThresholds(),
        passed_days=DAYS, partitions=split_chronologically(DAYS),
        dataset_sha256="9127a8bf" * 8, dataset_bytes=83_318_033,
        request_digest="req-digest")
    return replace(base, **overrides) if overrides else base


# --- cross-process stability ------------------------------------------

SUBPROCESS_SOURCE = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, {root!r})
    from datetime import date, timedelta
    from trading_system.features.config import FeatureConfig
    from trading_system.features.engine import ENGINE_VERSION
    from trading_system.research.identity import ResearchIdentity
    from trading_system.research.partitions import split_chronologically
    from trading_system.research.preregistered import build_registry
    from trading_system.research.quality import QualityThresholds

    days = [date(2021, 9, 7) + timedelta(days=i) for i in range(40)]
    identity = ResearchIdentity.build(
        study_version="1.0.0", engine_version=ENGINE_VERSION,
        symbol="NQ.c.0", feature_config=FeatureConfig(),
        registry=build_registry(), thresholds=QualityThresholds(),
        passed_days=days, partitions=split_chronologically(days),
        dataset_sha256="9127a8bf" * 8, dataset_bytes=83318033,
        request_digest="req-digest")
    print(json.dumps({{
        "semantic": identity.digest(),
        "row": identity.as_row(),
        "chain_head": build_registry().provenance()["head"],
    }}))
""")


def _in_subprocess(env=None):
    result = subprocess.run(
        [sys.executable, "-c", SUBPROCESS_SOURCE.format(root=str(ROOT))],
        capture_output=True, text=True, timeout=180, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_semantic_digest_is_identical_in_two_separate_processes():
    """The property the whole module exists for."""
    a = _in_subprocess()
    b = _in_subprocess()
    assert a["semantic"] == b["semantic"]
    assert a["row"] == b["row"]


def test_it_is_also_identical_under_a_different_hash_seed():
    """Python randomises str hashing per process. Anything that leaked
    set or dict iteration order into the digest would show up here and
    nowhere else."""
    import os
    seeds = []
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        seeds.append(_in_subprocess(env)["semantic"])
    assert len(set(seeds)) == 1, f"digest varies with hash seed: {seeds}"


def test_the_hypothesis_chain_is_confirmed_unstable_as_the_reason_this_exists():
    """Not a defect being tolerated in silence: the chain SHOULD carry a
    registration time, and this pins why the semantic digest cannot be
    built from it."""
    a = _in_subprocess()
    b = _in_subprocess()
    assert a["chain_head"] != b["chain_head"], (
        "the chain head is now stable across processes; if registered_at "
        "was removed, that rewrote the frozen registry's history and this "
        "test should fail loudly rather than quietly pass")


def test_the_digest_matches_what_this_process_computes():
    assert _in_subprocess()["semantic"] == build().digest()


def test_it_is_stable_across_repeated_builds_in_one_process():
    assert build().digest() == build().digest()


# --- sensitivity: everything semantic must move it --------------------

@pytest.mark.parametrize("field", [
    "semantic_version", "study_version", "engine_version", "symbol",
    "feature_config", "feature_config_name", "hypotheses", "outcome_spec",
    "eligibility", "quality_gate", "partitions", "dataset", "code",
])
def test_changing_any_semantic_component_changes_the_digest(field):
    assert build().digest() != build(**{field: "moved"}).digest()


def test_the_identity_covers_every_component_the_ticket_named():
    """Guards against a field being dropped in a later refactor."""
    row = build().as_row()
    for required in ("hypotheses", "outcome_spec", "eligibility",
                     "quality_gate", "partitions", "dataset", "code",
                     "engine_version", "feature_config", "symbol",
                     "semantic_digest"):
        assert required in row, required


def test_editing_a_hypothesis_moves_the_digest_but_a_rerun_does_not():
    from trading_system.research.hypotheses import HypothesisRegistry
    base = registry_structure_digest(build_registry())
    assert base == registry_structure_digest(build_registry())
    altered = HypothesisRegistry()
    for h in build_registry().all():
        altered.register(h.id, h.statement, h.event_type, h.direction,
                         h.horizon_minutes + (1 if h.id == "H6" else 0),
                         dict(h.conditions), h.falsifier)
    assert registry_structure_digest(altered) != base


def test_reordering_the_hypotheses_is_a_different_design():
    """Benjamini-Hochberg ranks across the registered set, so order is
    not cosmetic."""
    from trading_system.research.hypotheses import HypothesisRegistry
    forward = build_registry()
    backward = HypothesisRegistry()
    for h in reversed(forward.all()):
        backward.register(h.id, h.statement, h.event_type, h.direction,
                          h.horizon_minutes, dict(h.conditions), h.falsifier)
    assert registry_structure_digest(backward) != \
        registry_structure_digest(forward)


def test_the_outcome_specification_is_part_of_the_identity():
    """A different bootstrap seed is a different confidence interval, and
    an interval decides whether a hypothesis is REJECTED."""
    assert outcome_spec_digest() != outcome_spec_digest(bootstrap_seed=1)
    assert outcome_spec_digest() != outcome_spec_digest(fdr=0.10)
    assert outcome_spec_digest() != outcome_spec_digest(bootstrap_iterations=500)
    assert outcome_spec_digest() != outcome_spec_digest(bootstrap_confidence=0.99)


def test_the_eligibility_rule_is_part_of_the_identity(monkeypatch):
    import trading_system.research.identity as mod
    before = eligibility_digest()
    monkeypatch.setattr(mod, "PRIOR_SESSION_EVENTS",
                        frozenset({"liquidity_sweep_high"}))
    assert eligibility_digest() != before


def test_the_quality_gate_verdict_is_part_of_the_identity():
    assert quality_digest(QualityThresholds(), DAYS) != \
        quality_digest(QualityThresholds(), DAYS[:-1])
    loosened = replace(QualityThresholds(), max_rth_gap_minutes=45)
    assert quality_digest(loosened, DAYS) != \
        quality_digest(QualityThresholds(), DAYS)


def test_the_code_digest_covers_the_modules_that_compute():
    for essential in ("research/outcomes.py", "research/study.py",
                      "research/eligibility.py", "research/identity.py",
                      "features/engine.py", "research/preregistered.py",
                      "research/quality.py", "research/inference.py"):
        assert essential in COMPUTING_MODULES, essential
    for rel in COMPUTING_MODULES:
        assert (ROOT / "trading_system" / rel).exists(), rel


def test_a_missing_module_fails_closed(tmp_path):
    (tmp_path / "features").mkdir()
    with pytest.raises(IdentityError, match="missing"):
        code_digest(tmp_path)


def test_wall_clock_metadata_is_absent_from_the_identity():
    """The exclusion is the point. If a timestamp ever reaches this row
    the digest stops being comparable and nothing else would say so."""
    blob = json.dumps(build().as_row())
    for banned in ("registered_at", "started_at", "created_at", "updated_at",
                   "completed_at", "run_id", "captured_at"):
        assert banned not in blob, banned


# --- the study reports it, and its own digest is now stable -----------

STUDY_SOURCE = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, {root!r})
    sys.path.insert(0, {tests!r})
    from datetime import date, timedelta
    from trading_system.features.contracts import ContractTimeline
    from trading_system.features.engine import FeatureEngine
    from trading_system.research.partitions import Partition, split_chronologically
    from trading_system.research.preregistered import build_registry
    from trading_system.research.quality import QualityReport
    from trading_system.research.study import Study
    from fixtures_market import NQ, synthetic_sessions

    days = synthetic_sessions(3, minutes=700)
    bars = [b for i in sorted(days) for b in days[i]]
    engine = FeatureEngine(NQ)
    events, features = [], []
    for i in sorted(days):
        for r in engine.run(days[i]):
            (events if r.kind.value == "event" else features).append(r)
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(3)]
    registry = build_registry()
    study = Study("t", split_chronologically(day_list), registry,
                  QualityReport(),
                  contracts=ContractTimeline.from_bars(
                      bars, FeatureEngine(NQ).calendar))
    for h in registry.all():
        study.test(h.id, Partition.DISCOVERY, events, features, bars)
    print(json.dumps({{"digest": study.reproducibility_digest()}}))
""")


def _study_digest_in_subprocess():
    result = subprocess.run(
        [sys.executable, "-c", STUDY_SOURCE.format(
            root=str(ROOT), tests=str(ROOT / "tests"))],
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["digest"]


def test_the_reproducibility_digest_is_now_stable_across_processes():
    """The regression that started this. Two interpreters, identical code
    and data, identical digest."""
    assert _study_digest_in_subprocess() == _study_digest_in_subprocess()


def test_a_study_reports_its_identity_in_provenance():
    from trading_system.research.study import Study
    study = Study("t", split_chronologically(DAYS), build_registry(),
                  QualityReport(), identity=build())
    prov = study.provenance()
    assert prov["research_identity"]["semantic_digest"] == build().digest()
    # The audit chain is still reported in full beside it.
    assert prov["hypotheses"]["chain_valid"] is True
    assert prov["hypotheses"]["count"] == 12
    assert all("hash" in h for h in prov["hypotheses"]["hypotheses"])


def test_the_runner_records_the_identity_in_the_report():
    import ast
    source = (ROOT / "scripts" / "run_t004_study.py").read_text()
    assert "ResearchIdentity.build" in source
    assert "research_identity" in source
    assert "CheckpointKey.for_identity" in source
