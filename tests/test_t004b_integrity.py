"""T-004B must not be mistaken for T-004, or for confirmatory evidence.

The methodology was revised after the original NQ results were seen.
That ordering is the whole reason these constraints exist: a comparison
chosen once you know how the first one turned out is a second look at
the same data, and the only thing standing between "diagnostic" and
"confirmatory" is that the difference is stated everywhere it could be
misread.
"""
import ast
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from trading_system.features.config import FeatureConfig
from trading_system.features.engine import ENGINE_VERSION
from trading_system.research.baseline import BASELINE_VERSION, MatchingSpec
from trading_system.research.checkpoint import CheckpointError, CheckpointKey, \
    StudyCheckpoint
from trading_system.research.identity import ResearchIdentity
from trading_system.research.partitions import split_chronologically
from trading_system.research.preregistered import build_registry
from trading_system.research.quality import QualityThresholds

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_t004b_baseline.py"
T004 = ROOT / "scripts" / "run_t004_study.py"
DAYS = [date(2021, 9, 7) + timedelta(days=i) for i in range(40)]


def identity(analysis="T-004", baseline="none"):
    return ResearchIdentity.build(
        study_version="1.0.0", engine_version=ENGINE_VERSION, symbol="NQ.c.0",
        feature_config=FeatureConfig(), registry=build_registry(),
        thresholds=QualityThresholds(), passed_days=DAYS,
        partitions=split_chronologically(DAYS),
        dataset_sha256="9127a8bf" * 8, dataset_bytes=83_318_033,
        request_digest="req", analysis_version=analysis,
        baseline_digest=baseline)


# --- the holdout ------------------------------------------------------

def test_t004b_has_no_way_to_open_the_holdout():
    """T-004 has --unseal-holdout behind a recorded reason. A diagnostic
    re-analysis of results already seen is the last thing that should be
    able to reach the final sample, so the flag simply does not exist."""
    def flags(path):
        tree = ast.parse(path.read_text())
        out = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                out.update(a.value for a in node.args
                           if isinstance(a, ast.Constant))
        return out

    # Checked against the PARSED arguments, not the text: the module
    # docstring explains that the flag is deliberately absent, and a
    # substring search cannot tell an explanation from an option.
    assert not any("unseal" in f for f in flags(RUNNER))
    assert any("unseal" in f for f in flags(T004)), (
        "the T-004 runner should still have it; if not, this test is "
        "comparing against the wrong thing")

    tree = ast.parse(RUNNER.read_text())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "unseal" not in called, "the runner calls partitions.unseal"


def test_t004b_selects_only_discovery_and_validation():
    tree = ast.parse(RUNNER.read_text())
    referenced = {n.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute)
                  and isinstance(n.value, ast.Name)
                  and n.value.id == "Partition"}
    assert referenced == {"DISCOVERY", "VALIDATION"}, referenced


def test_the_report_records_that_the_holdout_was_not_read():
    source = RUNNER.read_text()
    assert '"holdout_read": False' in source


# --- not confirmatory -------------------------------------------------

def test_the_report_is_labelled_diagnostic_everywhere_it_could_be_misread():
    source = RUNNER.read_text()
    for required in ('"confirmatory": False', '"preregistered": False',
                     '"supersedes_t004": False', "ANALYSIS_VERSION",
                     "diagnostic/exploratory"):
        assert required in source, required


def test_the_status_string_says_why_it_is_diagnostic():
    import importlib.util
    spec = importlib.util.spec_from_file_location("t004b", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ANALYSIS_VERSION == "T-004B"
    assert "after seeing T-004" in module.STATUS
    assert module.STATUS.startswith("diagnostic")


# --- the T-004 report is immutable ------------------------------------

def test_it_refuses_to_overwrite_a_t004_report(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("t004b", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    victim = tmp_path / "t004_nq.json"
    victim.write_text(json.dumps(
        {"research_identity": {"analysis_version": "T-004"}}))
    with pytest.raises(SystemExit, match="must not overwrite"):
        module.refuse_to_overwrite_t004(victim)


def test_it_will_overwrite_its_own_previous_output(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("t004b", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    own = tmp_path / "t004b_nq.json"
    own.write_text(json.dumps(
        {"research_identity": {"analysis_version": "T-004B"}}))
    module.refuse_to_overwrite_t004(own)      # must not raise


def test_a_missing_or_unreadable_file_is_not_treated_as_a_t004_report(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("t004b", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.refuse_to_overwrite_t004(tmp_path / "absent.json")
    junk = tmp_path / "junk.json"
    junk.write_text("not json {{{")
    module.refuse_to_overwrite_t004(junk)


# --- the two analyses cannot be confused -------------------------------

def test_t004_and_t004b_have_different_identities():
    a = identity("T-004", "none")
    b = identity("T-004B", MatchingSpec().digest())
    assert a.digest() != b.digest()
    assert set(a.differences(b)) == {"analysis_version", "baseline"}


def test_a_t004_checkpoint_cannot_be_resumed_by_t004b(tmp_path):
    """Same data, same hypotheses, different question. Resuming across
    them would splice results from one analysis into the other."""
    path = tmp_path / "c.json"
    StudyCheckpoint(key=CheckpointKey.for_identity(identity("T-004"))).save(path)
    later = CheckpointKey.for_identity(
        identity("T-004B", MatchingSpec().digest()))
    with pytest.raises(CheckpointError) as exc:
        StudyCheckpoint.load(path, later)
    assert "analysis_version" in str(exc.value)


def test_changing_the_matching_rule_is_a_different_analysis():
    """A matching spec chosen after seeing which one gave a nicer lift
    would be the same snooping one level down; the identity makes such a
    change visible instead of silent."""
    a = identity("T-004B", MatchingSpec().digest())
    b = identity("T-004B", MatchingSpec(session_minute_bucket=15).digest())
    assert a.digest() != b.digest()
    assert a.differences(b) == ["baseline"]


def test_the_baseline_module_is_in_the_code_digest():
    from trading_system.research.identity import COMPUTING_MODULES
    assert "research/baseline.py" in COMPUTING_MODULES


# --- the frozen set is untouched --------------------------------------

def test_t004b_does_not_alter_any_hypothesis():
    source = RUNNER.read_text()
    assert "register(" not in source
    assert "build_registry()" in source
    registry = build_registry()
    assert registry.count() == 12 and registry.verify() and registry.sealed


def test_the_writer_is_atomic():
    """A crash mid-write must not leave a file that parses far enough to
    look finished -- the failure that cost a session already."""
    tree = ast.parse(RUNNER.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "atomic_write")
    body = ast.dump(fn)
    assert "fsync" in body and "replace" in body


def test_the_documented_t005_dependency_exists():
    doc = ROOT / "docs" / "BASELINE-METHODOLOGY.md"
    assert doc.exists(), "the T-005 discovery target must be written down"
    text = doc.read_text()
    assert "T-005" in text
    assert "lift" in text.lower()
    assert BASELINE_VERSION in text
