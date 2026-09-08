"""The artifact verifier.

It answers "did the study actually finish?" after a crash, so its
failure modes matter more than its happy path. A verifier that passes a
truncated report is worse than none: it converts an unknown into a
false certainty.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VERIFIER = ROOT / "scripts" / "verify_t004_artifacts.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify", VERIFIER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _load()


def _good_report(sha="a" * 64):
    results = [{"partition": p, "hypothesis_id": f"H{i}"}
               for p in ("discovery", "validation") for i in range(1, 13)]
    return {
        "manifest": {"sha256": sha, "file": "t004_ohlcv_1m.dbn.zst"},
        "provenance": {
            "study": "t004-NQ.c.0", "study_version": "1.0.0",
            "started_at": "2026-09-08T09:00:00+00:00",
            "partitions": {"discovery": ["2021-09-01", "2024-01-01"],
                           "validation": ["2024-01-02", "2025-06-01"],
                           "holdout": ["2025-06-02", "2026-09-01"],
                           "holdout_ever_unsealed": False, "unseals": []},
            "hypotheses": {"count": 12, "sealed": True, "chain_valid": True},
            "data_quality": {"days_assessed": 1256, "days_passed": 561,
                             "days_excluded": 695},
            "multiplicity": {"total_tests_run": 24},
            "results": results,
        },
        "verdicts": {f"H{i}": {"verdict": "reject", "reason": "x"}
                     for i in range(1, 13)},
    }


def _run(tmp_path, report=None, raw=None, with_data=True, sha=None):
    """Run the verifier in an isolated directory and return (code, out)."""
    if report is not None:
        (tmp_path / "t004_nq.json").write_text(json.dumps(report, indent=2))
    if raw is not None:
        (tmp_path / "t004_nq.json").write_text(raw)
    if with_data:
        d = tmp_path / "data" / "t004"
        d.mkdir(parents=True)
        payload = b"pretend dbn bytes"
        (d / "t004_ohlcv_1m.dbn.zst").write_bytes(payload)
        digest = sha or hashlib.sha256(payload).hexdigest()
        (d / "manifest.json").write_text(json.dumps({
            "file": "t004_ohlcv_1m.dbn.zst", "bytes": len(payload),
            "sha256": digest,
            "request": {"dataset": "GLBX.MDP3", "schema": "ohlcv-1m",
                        "symbols": ["NQ.c.0", "MNQ.c.0", "ES.c.0"],
                        "stype_in": "continuous",
                        "start": "2021-09-01", "end": "2026-09-01"},
            "request_digest": "",
        }))
    proc = subprocess.run([sys.executable, str(VERIFIER)], cwd=tmp_path,
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def test_a_missing_report_requires_a_rerun(tmp_path):
    code, out = _run(tmp_path)
    assert code == 1
    assert "did not reach its final write" in out


def test_a_truncated_report_is_detected_not_accepted(tmp_path):
    """The exact crash signature: a write cut off mid-file. It must not
    be mistaken for a finished run."""
    raw = json.dumps(_good_report(), indent=2)[: int(len(json.dumps(_good_report())) * 0.6)]
    code, out = _run(tmp_path, raw=raw)
    assert code == 1
    assert "truncated or corrupt" in out


def test_an_empty_report_is_detected(tmp_path):
    code, out = _run(tmp_path, raw="")
    assert code == 1
    assert "file is not empty" in out


def test_a_report_missing_hypotheses_is_incomplete(tmp_path):
    report = _good_report()
    report["verdicts"] = {"H1": {"verdict": "reject"}}
    code, out = _run(tmp_path, report=report)
    assert code == 1
    assert "a verdict for every hypothesis" in out


def test_a_broken_hypothesis_chain_fails(tmp_path):
    report = _good_report()
    report["provenance"]["hypotheses"]["chain_valid"] = False
    code, _ = _run(tmp_path, report=report)
    assert code == 1


def test_an_unsealed_holdout_is_reported(tmp_path):
    """If a crashed run had opened the holdout, that must surface here
    rather than being discovered later."""
    report = _good_report()
    report["provenance"]["partitions"]["holdout_ever_unsealed"] = True
    report["provenance"]["partitions"]["unseals"] = [{"reason": "curiosity"}]
    code, out = _run(tmp_path, report=report)
    assert code == 1
    assert "holdout never unsealed" in out


def test_holdout_results_present_is_a_failure(tmp_path):
    report = _good_report()
    report["provenance"]["results"].append({"partition": "holdout",
                                            "hypothesis_id": "H1"})
    code, out = _run(tmp_path, report=report)
    assert code == 1
    assert "no holdout results present" in out


def test_partial_files_are_reported(tmp_path):
    report = _good_report()
    (tmp_path / "t004_nq.json.partial").write_text("{")
    code, out = _run(tmp_path, report=report)
    assert code == 1
    assert "no partial/temp files" in out


def test_a_tampered_dataset_is_detected(tmp_path):
    code, out = _run(tmp_path, report=_good_report(), sha="b" * 64)
    assert code == 1
    assert "SHA-256 matches manifest" in out


def test_a_report_from_a_different_dataset_is_detected(tmp_path):
    """Guards against pairing yesterday's report with today's data."""
    code, out = _run(tmp_path, report=_good_report(sha="c" * 64))
    assert code == 1
    assert "produced from this exact dataset" in out


def test_the_verifier_never_writes_anything(tmp_path):
    """A verification tool that mutates state destroys the evidence it
    exists to examine."""
    _run(tmp_path, report=_good_report())
    before = {p.name for p in tmp_path.rglob("*")}
    _run(tmp_path, report=None, with_data=False)
    after = {p.name for p in tmp_path.rglob("*")}
    assert before == after


def test_source_contains_no_mutating_calls():
    src = VERIFIER.read_text()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    for banned in ("unlink(", "rmtree", "write_text(", "write_bytes(",
                   "os.remove", "os.replace", "shutil."):
        assert banned not in code, f"{banned} would mutate the evidence"
