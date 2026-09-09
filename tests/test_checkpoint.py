"""Checkpoint and resume: the five properties the ticket names.

  1. interrupted + resumed output == uninterrupted output, exactly
  2. a tampered checkpoint is rejected
  3. a stale checkpoint (code / config / data changed) is rejected
  4. a crash during the write cannot leave a valid-looking checkpoint
  5. the holdout stays sealed throughout

The danger a checkpoint introduces is not corruption, which is loud.
It is a SILENT splice: results computed under one version of the code or
the data, reloaded into a report describing another, with nothing in the
output to show it happened. Every test here is aimed at that.
"""
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from trading_system.features.config import FeatureConfig
from trading_system.features.contracts import ContractTimeline
from trading_system.features.engine import ENGINE_VERSION, FeatureEngine
from trading_system.provenance import digest
from trading_system.research.checkpoint import (
    CHECKPOINT_VERSION, CheckpointError, CheckpointKey, StudyCheckpoint,
    code_digest, partition_digest, quality_digest, registry_structure_digest)
from trading_system.research.partitions import (HoldoutSealed, Partition,
                                                split_chronologically)
from trading_system.research.preregistered import build_registry
from trading_system.research.quality import QualityReport, QualityThresholds
from trading_system.research.study import Study

from tests.fixtures_market import NQ, synthetic_sessions

SESSIONS = 6
MINUTES = 700


@pytest.fixture(scope="module")
def workload():
    days = synthetic_sessions(SESSIONS, minutes=MINUTES)
    bars = [b for i in sorted(days) for b in days[i]]
    engine = FeatureEngine(NQ)
    events, features = [], []
    for i in sorted(days):
        for r in engine.run(days[i]):
            (events if r.kind.value == "event" else features).append(r)
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(SESSIONS)]
    return days, bars, events, features, day_list


def make_key(day_list, **overrides) -> CheckpointKey:
    base = dict(
        checkpoint_version=CHECKPOINT_VERSION, study_version="1.0.0",
        engine_version=ENGINE_VERSION, symbol="NQ.c.0",
        dataset_sha256="9127a8bf" * 8, dataset_bytes=83_318_033,
        request_digest="req-digest", config_digest=FeatureConfig().digest(),
        config_name=FeatureConfig().name,
        registry_structure=registry_structure_digest(build_registry()),
        quality=quality_digest(QualityThresholds(), day_list),
        partitions=partition_digest(split_chronologically(day_list)),
        code=code_digest(),
    )
    base.update(overrides)
    return CheckpointKey(**base)


def _fresh_study(bars, day_list):
    return Study("t", split_chronologically(day_list), build_registry(),
                 QualityReport(),
                 contracts=ContractTimeline.from_bars(
                     bars, FeatureEngine(NQ).calendar))


# --- 1. resumed == uninterrupted --------------------------------------

def test_a_resumed_run_produces_exactly_what_an_uninterrupted_one_does(
        workload, tmp_path):
    days, bars, events, features, day_list = workload
    registry = build_registry()          # ONE registry, so the digests
                                         # are comparable across studies
    key = make_key(day_list)
    stages = [Partition.DISCOVERY, Partition.VALIDATION]

    # (a) straight through
    whole = Study("t", split_chronologically(day_list), registry,
                  QualityReport(),
                  contracts=ContractTimeline.from_bars(
                      bars, FeatureEngine(NQ).calendar))
    for stage in stages:
        for h in registry.all():
            whole.test(h.id, stage, events, features, bars)

    # (b) interrupted after discovery, then resumed in a new process's
    #     worth of state: a brand-new Study that has never seen a bar.
    first = Study("t", split_chronologically(day_list), registry,
                  QualityReport(),
                  contracts=ContractTimeline.from_bars(
                      bars, FeatureEngine(NQ).calendar))
    for h in registry.all():
        first.test(h.id, Partition.DISCOVERY, events, features, bars)
    ckpt = StudyCheckpoint(key=key)
    ckpt.record_stage(Partition.DISCOVERY,
                      [r for r in first.results
                       if r.partition == Partition.DISCOVERY.value],
                      first.ledger, len(day_list), len(events))
    path = tmp_path / "t004.checkpoint.json"
    ckpt.save(path)
    del first                            # the crash

    reloaded = StudyCheckpoint.load(path, key)
    resumed = Study("t", split_chronologically(day_list), registry,
                    QualityReport(),
                    contracts=ContractTimeline.from_bars(
                        bars, FeatureEngine(NQ).calendar))
    reloaded.restore_into(resumed, Partition.DISCOVERY)
    for h in registry.all():
        resumed.test(h.id, Partition.VALIDATION, events, features, bars)

    assert [r.as_row() for r in whole.results] == \
        [r.as_row() for r in resumed.results]
    assert whole.ledger.total_tests == resumed.ledger.total_tests
    assert whole.reproducibility_digest() == resumed.reproducibility_digest()
    a = {k: v.as_row() for k, v in whole.classify_all().items()}
    b = {k: v.as_row() for k, v in resumed.classify_all().items()}
    assert a == b


def test_full_precision_survives_the_round_trip(workload, tmp_path):
    """`as_row` rounds to six decimals; a confidence bound within 5e-7 of
    zero would change which side of zero it is on, and that decides a
    verdict. The checkpoint must store raw floats."""
    days, bars, events, features, day_list = workload
    study = _fresh_study(bars, day_list)
    for h in build_registry().all():
        study.test(h.id, Partition.DISCOVERY, events, features, bars)
    ckpt = StudyCheckpoint(key=make_key(day_list))
    ckpt.record_stage(Partition.DISCOVERY, study.results, study.ledger, 6, 1)
    path = tmp_path / "c.json"
    ckpt.save(path)
    back = StudyCheckpoint.load(path, make_key(day_list))
    for before, after in zip(study.results,
                             back.results_for(Partition.DISCOVERY)):
        assert before.estimate == after.estimate, before.hypothesis_id
        assert before.p_value == after.p_value
        assert before.eligibility == after.eligibility


# --- 2. tampering ------------------------------------------------------

def test_an_edited_checkpoint_is_rejected(tmp_path):
    key = make_key([date(2021, 9, 7) + timedelta(days=i) for i in range(6)])
    ckpt = StudyCheckpoint(key=key)
    ckpt.stages["discovery"] = {"results": [], "sessions": 6, "events": 10,
                                "completed_at": "2026-09-09T00:00:00+00:00"}
    path = tmp_path / "c.json"
    ckpt.save(path)

    data = json.loads(path.read_text())
    data["stages"]["discovery"]["events"] = 999_999      # a plausible edit
    path.write_text(json.dumps(data, indent=1, sort_keys=True))
    with pytest.raises(CheckpointError, match="does not match its own digest"):
        StudyCheckpoint.load(path, key)


def test_a_checkpoint_with_its_digest_stripped_is_rejected(tmp_path):
    key = make_key([date(2021, 9, 7) + timedelta(days=i) for i in range(6)])
    path = tmp_path / "c.json"
    StudyCheckpoint(key=key).save(path)
    data = json.loads(path.read_text())
    del data["body_digest"]
    path.write_text(json.dumps(data))
    with pytest.raises(CheckpointError, match="no body_digest"):
        StudyCheckpoint.load(path, key)


def test_a_forged_key_cannot_be_smuggled_past_its_own_digest(tmp_path):
    """Editing the key and its body_digest together still fails, because
    the key carries a digest of itself."""
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(6)]
    key = make_key(day_list)
    path = tmp_path / "c.json"
    StudyCheckpoint(key=key).save(path)

    data = json.loads(path.read_text())
    data["key"]["dataset_sha256"] = "0" * 64
    data.pop("body_digest")
    data["body_digest"] = digest(data)      # recompute the outer digest
    path.write_text(json.dumps(data, indent=1, sort_keys=True))
    with pytest.raises(CheckpointError, match="key does not match its digest"):
        StudyCheckpoint.load(path, key)


# --- 3. staleness ------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("dataset_sha256", "f" * 64),
    ("dataset_bytes", 83_318_034),
    ("request_digest", "a-different-acquisition"),
    ("symbol", "ES.c.0"),
    ("engine_version", "9.9.9"),
    ("config_digest", "a-different-config"),
    ("registry_structure", "a-different-hypothesis-set"),
    ("quality", "a-different-gate"),
    ("partitions", "a-different-split"),
    ("code", "a-different-build"),
    ("checkpoint_version", "0.0.1"),
])
def test_any_change_to_an_input_refuses_the_checkpoint(tmp_path, field, value):
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(6)]
    written_under = make_key(day_list)
    path = tmp_path / "c.json"
    StudyCheckpoint(key=written_under).save(path)

    now = make_key(day_list, **{field: value})
    with pytest.raises(CheckpointError) as exc:
        StudyCheckpoint.load(path, now)
    assert field in str(exc.value) or "format" in str(exc.value)


def test_the_key_names_exactly_what_changed(tmp_path):
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(6)]
    a = make_key(day_list)
    b = make_key(day_list, dataset_sha256="f" * 64, symbol="ES.c.0")
    assert a.differences(b) == ["dataset_sha256", "symbol"]


def test_a_quality_gate_that_passes_different_days_is_a_different_key():
    """The gate's verdict is part of the key, not just its settings."""
    days = [date(2021, 9, 7) + timedelta(days=i) for i in range(6)]
    assert quality_digest(QualityThresholds(), days) != \
        quality_digest(QualityThresholds(), days[:-1])


def test_a_changed_threshold_is_a_different_key():
    days = [date(2021, 9, 7) + timedelta(days=i) for i in range(6)]
    loosened = replace(QualityThresholds(), max_rth_gap_minutes=45)
    assert quality_digest(QualityThresholds(), days) != \
        quality_digest(loosened, days)


def test_editing_a_hypothesis_changes_the_registry_digest():
    """Timestamps must not enter it, but content must."""
    base = registry_structure_digest(build_registry())
    assert base == registry_structure_digest(build_registry()), (
        "the registry digest is not stable across two builds; it must not "
        "depend on registered_at")
    from trading_system.research.hypotheses import Direction, HypothesisRegistry
    altered = HypothesisRegistry()
    for h in build_registry().all():
        altered.register(h.id, h.statement, h.event_type, h.direction,
                         h.horizon_minutes + (1 if h.id == "H6" else 0),
                         dict(h.conditions), h.falsifier)
    assert registry_structure_digest(altered) != base


def test_the_code_digest_covers_every_module_that_computes(tmp_path):
    from trading_system.research.checkpoint import COMPUTING_MODULES
    root = Path(__file__).resolve().parents[1] / "trading_system"
    for rel in COMPUTING_MODULES:
        assert (root / rel).exists(), rel
    for essential in ("research/outcomes.py", "research/study.py",
                      "features/engine.py", "research/eligibility.py",
                      "research/quality.py", "research/preregistered.py"):
        assert essential in COMPUTING_MODULES
    assert code_digest() == code_digest()


def test_a_missing_module_fails_closed_rather_than_hashing_less(tmp_path):
    (tmp_path / "features").mkdir()
    with pytest.raises(CheckpointError, match="missing"):
        code_digest(tmp_path)


# --- 4. a crash during the write ---------------------------------------

def test_a_crash_mid_write_leaves_the_previous_checkpoint_intact(
        tmp_path, monkeypatch):
    """The write goes to a temp file and is renamed. A crash before the
    rename must leave the previous complete file, not a truncated one."""
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(6)]
    key = make_key(day_list)
    path = tmp_path / "c.json"

    good = StudyCheckpoint(key=key)
    good.stages["discovery"] = {"results": [], "sessions": 6, "events": 42,
                                "completed_at": "2026-09-09T00:00:00+00:00"}
    good.save(path)
    intact = path.read_bytes()

    import trading_system.research.checkpoint as mod

    def die(_fd):
        raise OSError("power loss")
    monkeypatch.setattr(mod.os, "fsync", die)

    doomed = StudyCheckpoint(key=key)
    doomed.stages["validation"] = {"results": [], "sessions": 6, "events": 7,
                                   "completed_at": "2026-09-09T01:00:00+00:00"}
    with pytest.raises(OSError):
        doomed.save(path)

    assert path.read_bytes() == intact, "the previous checkpoint was damaged"
    reloaded = StudyCheckpoint.load(path, key)
    assert reloaded.completed() == ["discovery"]
    assert reloaded.stages["discovery"]["events"] == 42


def test_a_truncated_checkpoint_cannot_look_valid(tmp_path):
    """Whatever produced it, a half-written file must not load."""
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(6)]
    key = make_key(day_list)
    path = tmp_path / "c.json"
    StudyCheckpoint(key=key).save(path)
    whole = path.read_text()
    for cut in (len(whole) // 2, len(whole) - 20, len(whole) - 1):
        path.write_text(whole[:cut])
        with pytest.raises(CheckpointError):
            StudyCheckpoint.load(path, key)


def test_the_partial_file_is_not_left_behind_on_success(tmp_path):
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(6)]
    path = tmp_path / "c.json"
    StudyCheckpoint(key=make_key(day_list)).save(path)
    assert not list(tmp_path.glob("*.partial"))


# --- 5. the holdout ----------------------------------------------------

def test_recording_a_holdout_stage_raises(workload, tmp_path):
    days, bars, events, features, day_list = workload
    ckpt = StudyCheckpoint(key=make_key(day_list))
    with pytest.raises(CheckpointError, match="refusing to checkpoint"):
        ckpt.record_stage(Partition.HOLDOUT, [], Study(
            "t", split_chronologically(day_list), build_registry(),
            QualityReport()).ledger, 1, 1)


def test_saving_a_checkpoint_containing_a_holdout_stage_raises(tmp_path):
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(6)]
    ckpt = StudyCheckpoint(key=make_key(day_list))
    ckpt.stages["holdout"] = {"results": [], "sessions": 1, "events": 1,
                              "completed_at": "2026-09-09T00:00:00+00:00"}
    with pytest.raises(CheckpointError, match="never be persisted"):
        ckpt.save(tmp_path / "c.json")


def test_a_checkpoint_file_containing_holdout_results_is_refused(tmp_path):
    """Not written by this code, so its presence means the file was
    constructed elsewhere. Refused outright rather than partially used."""
    day_list = [date(2021, 9, 7) + timedelta(days=i) for i in range(6)]
    key = make_key(day_list)
    path = tmp_path / "c.json"
    StudyCheckpoint(key=key).save(path)
    data = json.loads(path.read_text())
    data["stages"]["holdout"] = {"results": [], "sessions": 1, "events": 1,
                                 "completed_at": "2026-09-09T00:00:00+00:00"}
    data.pop("body_digest")
    data["body_digest"] = digest(data)
    path.write_text(json.dumps(data, indent=1, sort_keys=True))
    with pytest.raises(CheckpointError, match="holdout"):
        StudyCheckpoint.load(path, key)


def test_the_holdout_stays_sealed_across_an_interrupted_and_resumed_run(
        workload, tmp_path):
    days, bars, events, features, day_list = workload
    key = make_key(day_list)
    registry = build_registry()
    parts = split_chronologically(day_list)

    study = Study("t", parts, registry, QualityReport(),
                  contracts=ContractTimeline.from_bars(
                      bars, FeatureEngine(NQ).calendar))
    for h in registry.all():
        study.test(h.id, Partition.DISCOVERY, events, features, bars)
    ckpt = StudyCheckpoint(key=key)
    ckpt.record_stage(Partition.DISCOVERY, study.results, study.ledger, 6, 1)
    path = tmp_path / "c.json"
    ckpt.save(path)

    assert "holdout" not in path.read_text()

    resumed_parts = split_chronologically(day_list)
    resumed = Study("t", resumed_parts, registry, QualityReport(),
                    contracts=ContractTimeline.from_bars(
                        bars, FeatureEngine(NQ).calendar))
    StudyCheckpoint.load(path, key).restore_into(resumed, Partition.DISCOVERY)
    for h in registry.all():
        resumed.test(h.id, Partition.VALIDATION, events, features, bars)

    # Still sealed: asking for its days raises, and the provenance says so.
    with pytest.raises(HoldoutSealed):
        resumed_parts.select(day_list, Partition.HOLDOUT)
    prov = resumed.provenance()["partitions"]
    assert prov["holdout_ever_unsealed"] is False
    assert not prov.get("unseals")
    assert not any(r.partition == "holdout" for r in resumed.results)


# --- progress reporting ------------------------------------------------

def test_progress_reports_stage_position_percent_elapsed_and_eta():
    """The operator has to be able to tell a working run from a hung one
    without attaching a debugger to it."""
    from trading_system.research.progress import StageProgress
    now = [0.0]
    p = StageProgress("discovery", 100, min_interval=1e9, clock=lambda: now[0])
    for _ in range(25):
        now[0] += 4.0
        p.advance(1397, 740)
    line = p.line()
    assert "discovery" in line
    assert "25/100" in line
    assert "25.0%" in line
    assert "34,925 bars" in line
    assert "18,500 events" in line
    assert "elapsed" in line and "1m40s" in line
    assert "ETA" in line and "5m00s" in line      # 75 sessions x 4s
    assert "ckpt none" in line
    p.note_checkpoint("2026-09-09T11:22:33+00:00")
    assert "ckpt 11:22:33" in p.line()


def test_progress_is_rate_limited_so_it_cannot_flood_a_terminal():
    from trading_system.research.progress import StageProgress
    import io
    now = [0.0]
    out = io.StringIO()
    p = StageProgress("discovery", 5000, stream=out, min_interval=10.0,
                      clock=lambda: now[0])
    for _ in range(500):
        now[0] += 1.0
        p.advance(1397, 740)
    assert 40 <= len(out.getvalue().splitlines()) <= 60


def test_duration_formatting_is_readable_at_every_scale():
    from trading_system.research.progress import format_duration
    assert format_duration(7) == "7s"
    assert format_duration(95) == "1m35s"
    assert format_duration(3725) == "1h02m05s"
    assert format_duration(-5) == "0s"


# --- the runner wires it in -------------------------------------------

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "run_t004_study.py"


def test_the_runner_checkpoints_every_non_holdout_stage():
    import ast
    tree = ast.parse(RUNNER.read_text())
    names = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for required in ("record_stage", "save", "restore_into"):
        assert required in names, f"the runner never calls {required}"
    source = RUNNER.read_text()
    assert "load_or_start" in source
    assert "--restart" in source


def test_the_runner_passes_both_indexes_into_every_test():
    import ast
    tree = ast.parse(RUNNER.read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "test"]
    assert calls, "the runner never calls study.test"
    for call in calls:
        kw = {k.arg for k in call.keywords}
        assert "bar_index" in kw and "condition_index" in kw, (
            "study.test is called without an index; the run would take "
            "days rather than minutes")


def test_the_runner_does_not_retain_every_feature_record():
    """~17 features per bar over 1.7M bars is ~30M records held to
    consult one type. The runner must index as it streams."""
    source = RUNNER.read_text()
    assert "FeatureStateIndex" in source
    assert "required_condition_types" in source
    import ast
    tree = ast.parse(source)
    # No list named `features` accumulating engine output any more.
    assigned = {t.id for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assert "features" not in assigned, (
        "the runner still builds a features list")


def test_the_runner_removes_the_checkpoint_only_after_writing_the_report():
    """Ordering matters: unlinking first would make a crash during the
    report write lose the completed stages as well."""
    source = RUNNER.read_text()
    report_at = source.index("full report written to")
    unlink_at = source.index("ckpt_path.unlink()")
    assert report_at < unlink_at
