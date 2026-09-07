#!/usr/bin/env python3
"""Isolation guard: this repository must carry no runtime dependency on
the retired Command Center.

WHY A GUARD AND NOT A CONVENTION. The two systems share an author, a
vocabulary, and a set of good ideas. Copy-paste between them is expected
and legitimate -- docs/LEGACY-REUSE.md lists what was deliberately
reused. What must never come across is a live coupling: the old Supabase
project ref, a connection string, an import, or one of the organizational
table names whose FK chain made the old trading layer unusable in
isolation.

DOCUMENTATION IS EXEMPT, RUNTIME IS NOT. Naming the old system in a
design document is how we explain our own decisions; naming it in code or
configuration is how the isolation quietly dies. The split below is the
whole point of the guard.

Usage:
    check_no_legacy_dependency.py [TARGET_DIR]   # default: repository root
    check_no_legacy_dependency.py --selftest
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# The retired project's Supabase reference. Its presence anywhere in
# runtime code or configuration means this system can reach the frozen
# database, which is the single most important thing to prevent.
LEGACY_PROJECT_REF = "ofzpaoxnnsenkritmnyu"

LEGACY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(re.escape(LEGACY_PROJECT_REF), re.IGNORECASE),
     "retired Command Center Supabase project reference"),
    (re.compile(r"^\s*(import|from)\s+chief_of_staff\b", re.MULTILINE),
     "import of the retired chief_of_staff package"),
    (re.compile(r"\bchief_of_staff_(proc|dept)\b"),
     "retired Command Center database role"),
    (re.compile(r"\b(trading_intel|market_data_ingest)_(proc|dept|svc)\b"),
     "retired Command Center database role"),
    # The organizational abstractions this system deliberately does not have.
    # Word-bounded and schema-qualified forms both, so `trading.runs` (ours)
    # never collides with `agent_runs` (theirs).
    (re.compile(r"\b(agent_runs|attention_queue|approval_queue|context_facts"
                r"|conversation_turns|capability_policies|trade_interpretations"
                r"|interpretation_deliveries|raw_market_data|source_documents"
                r"|strategic_state|sps_funnel_snapshots|sps_content_drafts"
                r"|sps_funnel_interpretations)\b"),
     "retired Command Center table name"),
    (re.compile(r"\bdepartment_name\b|\bassigned_department\b"),
     "retired Command Center organizational column/enum"),
    (re.compile(r"(?i)\bCHIEF_[A-Z_]*(URL|KEY|TOKEN|MODEL|SECRET)\b"),
     "retired Command Center environment variable"),
    (re.compile(r"(?i)db\.ofzpaoxnnsenkritmnyu\.supabase\.co"),
     "retired Command Center database host"),
    (re.compile(r"(?i)\bCommand-Center\b(?![^\n]*(?:retired|frozen|legacy|formerly))"),
     "unqualified reference to the retired repository"),
]

# Secret names must be TRADING_-prefixed. A bare TELEGRAM_BOT_TOKEN or
# ANTHROPIC_API_KEY is exactly how two systems silently end up sharing one
# bot and one billing account -- the concrete risk the audit identified.
UNPREFIXED_SECRETS = re.compile(
    r"(?<![A-Z_])(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|ANTHROPIC_API_KEY"
    r"|UW_API_TOKEN|FRED_API_KEY|TV_WEBHOOK_SECRET|DATABASE_URL|SUPABASE_[A-Z_]+)\b"
)

# Prose may discuss the retired system freely; runtime may not. Anything
# not listed here is treated as runtime.
DOC_SUFFIXES = {".md", ".rst", ".txt"}
DOC_DIR_NAMES = {"docs"}

SKIP_DIR_NAMES = {".git", "venv", ".venv", "__pycache__", "node_modules",
                  ".pytest_cache", ".mypy_cache"}
SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".whl"}
SELF_PATH = Path(__file__).resolve()

# The one file whose JOB is to contain prohibited strings: the guard's own
# fixture tests. Excluded by EXACT PATH, never by pattern or directory, so
# every other test file remains fully in scope. Without this the guard
# cannot be tested at all, and an untested guard is a guess.
EXEMPT_PATHS = {
    (Path(__file__).resolve().parents[1] / "tests" / "test_guards.py"),
}



class Violation:
    def __init__(self, path: Path, line_no: int, line: str, reason: str):
        self.path, self.line_no, self.line, self.reason = path, line_no, line.strip(), reason

    def __str__(self) -> str:
        return f"{self.path}:{self.line_no}: {self.reason}\n    {self.line}"


def _is_documentation(path: Path) -> bool:
    if path.suffix.lower() in DOC_SUFFIXES:
        return True
    return any(part in DOC_DIR_NAMES for part in path.parts)


def scan_text(text: str, path: Path, *, documentation: bool = False) -> list[Violation]:
    violations: list[Violation] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if documentation:
            continue
        for pattern, reason in LEGACY_PATTERNS:
            if pattern.search(line):
                violations.append(Violation(path, line_no, line, reason))
        m = UNPREFIXED_SECRETS.search(line)
        if m:
            violations.append(Violation(
                path, line_no, line,
                f"unprefixed secret name {m.group(1)!r} -- use TRADING_{m.group(1)}",
            ))
    return violations


def scan_directory(target: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        if path.resolve() == SELF_PATH or path.resolve() in EXEMPT_PATHS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        violations.extend(scan_text(text, path, documentation=_is_documentation(path)))
    return violations


def _selftest() -> int:
    should_fail = [
        'SUPABASE_URL = "https://ofzpaoxnnsenkritmnyu.supabase.co"',
        "from chief_of_staff.db import ChiefDB",
        "cur.execute('select * from agent_runs')",
        "dsn = 'postgresql://chief_of_staff_proc@host/postgres'",
        "TELEGRAM_BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']",
        "ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')",
        "insert into attention_queue (summary) values (%s)",
        "assigned_department = 'trading_intel'",
        "CHIEF_SYNTHESIS_MODEL = 'x'",
    ]
    should_pass = [
        "TRADING_TELEGRAM_BOT_TOKEN = env.require('TRADING_TELEGRAM_BOT_TOKEN')",
        "TRADING_DATABASE_URL_ANALYST = env.get('TRADING_DATABASE_URL_ANALYST')",
        "cur.execute('select state from trading.system_state')",
        "insert into trading.runs (component_version_id) values (%s)",
        "from trading_system.provenance import ArtifactRecord",
        "stage = 'adversarial_review'",
        "TRADING_SUPABASE_URL = env.get('TRADING_SUPABASE_URL')",
    ]
    failures = []
    for text in should_fail:
        if not scan_text(text, Path("<selftest>")):
            failures.append(f"FALSE NEGATIVE (should have been flagged): {text!r}")
    for text in should_pass:
        hits = scan_text(text, Path("<selftest>"))
        if hits:
            failures.append(f"FALSE POSITIVE: {text!r} -> {hits[0].reason}")
    # Documentation exemption must actually work.
    if scan_text("we migrated away from ofzpaoxnnsenkritmnyu", Path("docs/X.md"),
                 documentation=True):
        failures.append("documentation exemption did not apply")
    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"SELFTEST PASSED: {len(should_fail)} true-positive cases, "
          f"{len(should_pass)} true-negative cases, documentation exemption verified.")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--selftest"]:
        return _selftest()
    target = Path(argv[0]) if argv else Path(__file__).resolve().parents[1]
    violations = scan_directory(target)
    if violations:
        print(f"LEGACY DEPENDENCY VIOLATION -- {len(violations)} finding(s):\n")
        for v in violations:
            print(v); print()
        print("This system must have no runtime dependency on the retired Command "
              "Center. Discuss it in docs/ if you need to; do not reference it from "
              "code or configuration.")
        return 1
    print(f"OK -- no Command Center runtime dependency found under {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
