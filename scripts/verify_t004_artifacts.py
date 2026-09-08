#!/usr/bin/env python3
"""Determine, from PERSISTED STATE ONLY, whether a T-004 study run
completed cleanly. Console history is not evidence.

Answers seven questions and returns a non-zero exit if a rerun is
needed, so the answer is a status code and not an impression.

Reads only. It never repairs, deletes or regenerates anything: a
verification tool that mutates state destroys the evidence it exists to
examine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXPECTED_TOP = {"manifest", "provenance", "verdicts"}
EXPECTED_PROVENANCE = {"study", "study_version", "started_at", "partitions",
                       "hypotheses", "data_quality", "multiplicity", "results"}
EXPECTED_HYPOTHESES = 12
EXPECTED_PARTITIONS = {"discovery", "validation"}      # holdout must be absent

# Anything matching these is debris from an interrupted or synced write.
PARTIAL_GLOBS = ("*.partial", "*.tmp", "*.temp", "*~", "*.swp",
                 "*-conflict*", "*conflicted copy*", "*.crdownload")

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


class Findings:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        mark = OK if condition else BAD
        print(f"[{mark}] {label}" + (f"  -- {detail}" if detail else ""))
        if not condition:
            self.failures.append(label)
        return condition

    def warn(self, condition: bool, label: str, detail: str = "") -> None:
        if not condition:
            print(f"[{WARN}] {label}" + (f"  -- {detail}" if detail else ""))
            self.warnings.append(label)
        else:
            print(f"[{OK}] {label}" + (f"  -- {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="t004_nq.json")
    ap.add_argument("--data-dir", default="data/t004")
    ap.add_argument("--symbol", default="NQ.c.0")
    args = ap.parse_args()

    f = Findings()
    report_path = Path(args.report)
    data_dir = Path(args.data_dir)

    print(f"verifying {report_path}  (symbol {args.symbol})\n")

    # -- 1. existence --------------------------------------------------
    print("1. EXISTENCE")
    exists = f.check(report_path.exists(), f"{report_path} exists")
    if not exists:
        print("\n   The run did not reach its final write. Nothing to inspect.")
    else:
        size = report_path.stat().st_size
        mtime = datetime.fromtimestamp(report_path.stat().st_mtime, tz=timezone.utc)
        f.check(size > 0, "file is not empty", f"{size:,} bytes")
        print(f"        last modified {mtime.isoformat()}")

    # -- 2. parseable and complete -------------------------------------
    print("\n2. COMPLETE AND PARSEABLE")
    report = None
    if exists:
        raw = report_path.read_text(encoding="utf-8", errors="replace")
        try:
            report = json.loads(raw)
            f.check(True, "parses as JSON")
        except json.JSONDecodeError as exc:
            f.check(False, "parses as JSON",
                    f"truncated or corrupt at line {exc.lineno} col {exc.colno}")
        if report is not None:
            f.check(raw.rstrip().endswith("}"), "ends with a closing brace")
            missing = EXPECTED_TOP - set(report)
            f.check(not missing, "has all top-level sections",
                    f"missing {sorted(missing)}" if missing else "manifest, provenance, verdicts")

    # -- 3. provenance and run metadata --------------------------------
    print("\n3. PROVENANCE AND RUN METADATA")
    prov = (report or {}).get("provenance") or {}
    if prov:
        missing = EXPECTED_PROVENANCE - set(prov)
        f.check(not missing, "provenance is complete",
                f"missing {sorted(missing)}" if missing else
                f"study={prov.get('study')} v{prov.get('study_version')}")
        hyp = prov.get("hypotheses", {})
        f.check(hyp.get("count") == EXPECTED_HYPOTHESES,
                f"{EXPECTED_HYPOTHESES} hypotheses recorded", f"got {hyp.get('count')}")
        f.check(hyp.get("chain_valid") is True, "hypothesis chain valid")
        f.check(hyp.get("sealed") is True, "hypothesis registry sealed")
        verdicts = (report or {}).get("verdicts") or {}
        f.check(len(verdicts) == EXPECTED_HYPOTHESES,
                "a verdict for every hypothesis", f"{len(verdicts)} verdicts")
        results = prov.get("results", [])
        tested = {r.get("partition") for r in results}
        f.check(tested == EXPECTED_PARTITIONS,
                "discovery and validation both ran", f"partitions present: {sorted(tested)}")
        expected_results = EXPECTED_HYPOTHESES * len(EXPECTED_PARTITIONS)
        f.check(len(results) == expected_results,
                f"{expected_results} results recorded", f"got {len(results)}")
        ledger = prov.get("multiplicity", {})
        f.check(ledger.get("total_tests_run") == expected_results,
                "snooping ledger agrees with results",
                f"ledger {ledger.get('total_tests_run')} vs {len(results)}")

        # Contract-boundary eligibility. A report with no eligibility
        # record came from the code path that had no rule, so its
        # prior-session results cannot be distinguished from
        # cross-contract artifacts and it must not read as valid.
        cp = prov.get("contract_provenance")
        f.check(cp is not None, "contract provenance recorded",
                "absent: this report predates the contract-boundary rule"
                if cp is None else
                f"{cp.get('distinct_contracts')} contracts, "
                f"{len(cp.get('transitions', []))} transitions")
        rows = [r.get("eligibility") for r in results]
        f.check(all(e is not None for e in rows),
                "every result carries an eligibility record",
                f"{sum(1 for e in rows if e is None)} of {len(rows)} missing")
        applied = [e for e in rows if e and e.get("applies")]
        f.check(bool(applied),
                "the rule applied to the prior-session hypotheses",
                f"{len(applied)} results scoped by "
                f"{applied[0]['rule'] if applied else 'nothing'}")
        blind = [e for e in applied
                 if e.get("excluded_by_verdict", {}).get("unknown_provenance")]
        f.check(not blind,
                "no result was refused for missing provenance",
                f"{len(blind)} results could not see a contract id at all")
        dq = prov.get("data_quality", {})
        if dq:
            print(f"        data quality: {dq.get('days_passed')}/"
                  f"{dq.get('days_assessed')} sessions passed, "
                  f"{dq.get('days_excluded')} excluded")
    else:
        f.check(False, "provenance present")

    # -- 4. holdout ----------------------------------------------------
    print("\n4. HOLDOUT")
    parts = prov.get("partitions", {}) if prov else {}
    if parts:
        f.check(parts.get("holdout_ever_unsealed") is False,
                "holdout never unsealed")
        f.check(not parts.get("unseals"), "no unseal records",
                f"{len(parts.get('unseals') or [])} recorded")
        f.check("holdout" not in {r.get("partition") for r in prov.get("results", [])},
                "no holdout results present")
        print(f"        holdout range {parts.get('holdout')}  (untouched)")
    else:
        f.check(False, "partition record present")

    # -- 5. partial / temp debris --------------------------------------
    print("\n5. PARTIAL OR TEMPORARY ARTIFACTS")
    debris = []
    for base in (Path("."), data_dir):
        if not base.exists():
            continue
        for pattern in PARTIAL_GLOBS:
            debris.extend(p for p in base.glob(pattern) if p.is_file())
    empty = [p for p in Path(".").glob("t004_*.json")
             if p.is_file() and p.stat().st_size == 0]
    f.check(not debris, "no partial/temp files",
            ", ".join(str(p) for p in debris) if debris else "none found")
    f.check(not empty, "no zero-byte study outputs",
            ", ".join(str(p) for p in empty) if empty else "none found")

    # -- 6. dataset and manifest integrity ------------------------------
    print("\n6. DATASET AND MANIFEST INTEGRITY")
    manifest_path = data_dir / "manifest.json"
    if f.check(manifest_path.exists(), f"{manifest_path} exists"):
        manifest = json.loads(manifest_path.read_text())
        dbn = data_dir / manifest["file"]
        if f.check(dbn.exists(), f"{manifest['file']} exists"):
            actual_size = dbn.stat().st_size
            f.check(actual_size == manifest["bytes"], "byte count matches manifest",
                    f"{actual_size:,} vs {manifest['bytes']:,}")
            print("        hashing the dataset (this takes a moment) ...")
            digest = hashlib.sha256(dbn.read_bytes()).hexdigest()
            f.check(digest == manifest["sha256"], "SHA-256 matches manifest",
                    f"{digest[:16]}... vs {manifest['sha256'][:16]}...")
        # the manifest must still describe the APPROVED acquisition
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "acq", Path(__file__).resolve().parent / "acquire_t004_dataset.py")
            acq = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(acq)
            req = manifest.get("request", {})
            f.check(acq.request_digest(req) == manifest.get("request_digest"),
                    "manifest request digest is self-consistent")
            acq.revalidate(req)
            f.check(True, "manifest still matches the approved acquisition")
        except Exception as exc:                          # noqa: BLE001
            f.check(False, "manifest still matches the approved acquisition",
                    f"{type(exc).__name__}: {exc}")
        # and the report, if present, must reference the SAME dataset
        if report and report.get("manifest"):
            f.check(report["manifest"].get("sha256") == manifest.get("sha256"),
                    "report was produced from this exact dataset")

    # -- 7. verdict ----------------------------------------------------
    print("\n" + "=" * 62)
    if f.failures:
        print(f"RERUN REQUIRED. {len(f.failures)} check(s) failed:")
        for name in f.failures:
            print(f"  - {name}")
        print("\nThe study did NOT complete cleanly, or its inputs changed.")
        return 1
    print("NO RERUN NEEDED for this symbol.")
    print("The report is complete, internally consistent, produced from the")
    print("verified dataset, and the holdout was never opened.")
    if f.warnings:
        print(f"\n{len(f.warnings)} warning(s) worth reading above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
