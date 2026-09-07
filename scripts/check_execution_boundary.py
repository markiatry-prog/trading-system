#!/usr/bin/env python3
"""Execution-boundary guard -- the non-negotiable air gap.

COPIED from the retired Command Center
(trading-intelligence/scripts/check_execution_boundary.py) and expanded
for this repository. See docs/LEGACY-REUSE.md. It is reused rather than
rewritten because its central design decision was already right: it
matches execution CAPABILITY SIGNATURES, not vendor names, so a read-only
market-data integration that legitimately names a brokerage does not trip
it while a genuine order call does.

THE RULE. This system must never possess, retrieve, reference, invoke, or
gain access to credentials, secret-store entries, API scopes, browser
sessions, SDK methods, functions, endpoints, or modules capable of
placing, modifying, cancelling, or otherwise executing trades/orders.

Market-data connectivity is permitted and expected in later tickets --
including from a brokerage, where that is the best available data source.
Order execution is not, at any point, under any ticket.

Deliberately narrower than a blanket broker-name string match: a broker
name appearing in a comment, README, test fixture, or read-only
market-data reference is not a violation, and must not fail this check
-- Trading Intelligence's own read-only market-data work legitimately
needs to talk about brokers and instruments. What this checks for is
genuine execution-capability *signatures* -- order-verb function/method
calls, known execution or browser-automation SDK imports, and
credential/endpoint names that combine a broker/order/execution term
with a secret- or endpoint-like suffix -- never mere word occurrence.

Usage:
    check_execution_boundary.py [TARGET_DIR]   # scan (default: repository root)
    check_execution_boundary.py --selftest     # run the guard's own fixture tests
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Each pattern is deliberately shaped like code (a function/method call, an
# import statement, a credential-name assembly), not a bare word, so a
# broker name in prose -- a comment, a README, a docstring -- never matches
# any of these on its own.
DENY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"\b(place|submit|send|create|enter)_?order\b", re.IGNORECASE),
        "order-placement function/method name",
    ),
    (
        re.compile(r"\b(cancel|modify|amend|replace)_?order\b", re.IGNORECASE),
        "order-modification/cancellation function/method name",
    ),
    (
        re.compile(r"\bclose_?position\b", re.IGNORECASE),
        "position-closing function/method name",
    ),
    (
        re.compile(
            r"\.(placeOrder|submitOrder|cancelOrder|modifyOrder|closePosition)\s*\(",
        ),
        "order-capable SDK method call",
    ),
    (
        re.compile(
            r"^\s*(import|from)\s+(tradovate|ib_insync|ibapi|alpaca_trade_api"
            r"|ccxt|MetaTrader5|oandapyV20)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
        "import of a known broker/execution SDK",
    ),
    (
        re.compile(
            r"^\s*(import|from)\s+(playwright|selenium|pyppeteer)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
        "import of a browser-automation library -- no legitimate use in a"
        " read-only intelligence pipeline",
    ),
    (
        re.compile(
            r"(TRADOVATE|BROKER|EXECUTION|ORDER)[_A-Z0-9]*?"
            r"(API[_-]?KEY|SECRET|TOKEN|PASSWORD|CRED(?:ENTIAL)?S?)",
            re.IGNORECASE,
        ),
        "credential name combining a broker/order/execution term with a"
        " secret-like suffix",
    ),
    (
        re.compile(
            r"(?i)/(place[_-]?order|cancel[_-]?order|modify[_-]?order"
            r"|orders?/(create|cancel|modify))\b",
        ),
        "order-related endpoint path",
    ),
    # --- additions for this repository (T-001) ---
    (
        re.compile(r"\b(buy|sell|short|cover|liquidate)_?(order|position)\b", re.IGNORECASE),
        "directional order/position action",
    ),
    (
        # NB the optional `all_?` segment: `flatten_all_positions` has no
        # word boundary after "all" (the next char is an underscore), so a
        # naive alternation silently misses the most common spelling.
        re.compile(r"\b(flatten|square_?off)_?(all_?)?positions?\b", re.IGNORECASE),
        "position-flattening call",
    ),
    (
        re.compile(r"\bbracket_?order\b|\boco_?order\b|\bstop_?loss_?order\b", re.IGNORECASE),
        "compound order type",
    ),
    (
        re.compile(
            r"^\s*(import|from)\s+(tradovate_api|tastytrade|schwab|tda|ibkr"
            r"|robinhood|kiteconnect|binance|coinbase)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
        "import of a brokerage/exchange SDK",
    ),
    (
        re.compile(r"(?i)\b(account|trading|order)[_-]?(session|auth)[_-]?(token|secret)\b"),
        "trading-session credential name",
    ),
]

SKIP_DIR_NAMES = {".git", "venv", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"}
SKIP_SUFFIXES = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".whl",
}
# The guard's own file necessarily contains these pattern strings and
# fixture cases to define what it looks for -- that's the security tooling
# itself, not Trading Intelligence application/runtime code, and scanning
# it would trip every one of its own patterns on every run. Excluded by
# exact path, not by directory, so any *other* script under
# trading-intelligence/scripts/ (e.g. a future deployment helper) is still
# fully in scope.
SELF_PATH = Path(__file__).resolve()

# The one file whose JOB is to contain prohibited strings: the guard's own
# fixture tests. Excluded by EXACT PATH, never by pattern or directory, so
# every other test file remains fully in scope. Without this the guard
# cannot be tested at all, and an untested guard is a guess.
EXEMPT_PATHS = {
    (Path(__file__).resolve().parents[1] / "tests" / "test_guards.py"),
}



class Violation:
    def __init__(self, path: Path, line_no: int, line: str, reason: str) -> None:
        self.path = path
        self.line_no = line_no
        self.line = line.strip()
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.path}:{self.line_no}: {self.reason}\n    {self.line}"


def _iter_files(target: Path):
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        if path.resolve() == SELF_PATH or path.resolve() in EXEMPT_PATHS:
            continue
        yield path


def scan_text(text: str, path: Path) -> list[Violation]:
    violations: list[Violation] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern, reason in DENY_PATTERNS:
            if pattern.search(line):
                violations.append(Violation(path, line_no, line, reason))
    return violations


def scan_directory(target: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in _iter_files(target):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable -- not a source of executable code text
        violations.extend(scan_text(text, path))
    return violations


def _selftest() -> int:
    """Fixture-based proof the guard catches real capability and ignores
    plain broker-name references -- run in CI so the guard's own
    correctness is checked, not just assumed."""
    cases_should_fail = [
        "def place_order(symbol, qty): ...",
        "response = client.placeOrder(order)",
        "import tradovate",
        "from ib_insync import IB",
        "import selenium",
        "TRADOVATE_API_SECRET = os.environ['TRADOVATE_API_SECRET']",
        "resp = requests.post('https://api.example.com/orders/create', json=payload)",
        "def cancel_order(order_id): ...",
        "res = api.buy_order(symbol='MNQ', qty=1)",
        "flatten_all_positions()",
        "submit = client.bracket_order(...)",
        "import tastytrade",
        "TRADING_SESSION_TOKEN = os.environ['TRADING_SESSION_TOKEN']",
    ]
    cases_should_pass = [
        "# we researched tradovate's read-only custom-indicator JS SDK",
        "README: this pipeline never touches Tradovate order execution.",
        "gamma_exposure = fetch_unusual_whales_gamma()",
        "# order book depth is a read-only market-data concept, not an order",
        "def send_briefing_to_telegram(text): ...",
        "BROKER_NAME = 'tradovate'  # documentation only, not a credential",
        # Market data is permitted -- these must NOT trip the guard, or the
        # ingestion ticket becomes impossible to write.
        "quotes = tradovate_market_data.get_bars('MNQ', '1m')",
        "TRADING_UW_API_TOKEN = env.require('TRADING_UW_API_TOKEN')",
        "# order book imbalance is a market-data feature, not an order",
        "def fetch_vix_term_structure(): ...",
        "observed_at = bar.close_time  # market data provenance",
    ]

    failures = []
    for text in cases_should_fail:
        if not scan_text(text, Path("<selftest>")):
            failures.append(f"FALSE NEGATIVE (should have been flagged): {text!r}")
    for text in cases_should_pass:
        hits = scan_text(text, Path("<selftest>"))
        if hits:
            failures.append(
                f"FALSE POSITIVE (should not have been flagged): {text!r} -> {hits[0].reason}"
            )

    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"SELFTEST PASSED: {len(cases_should_fail)} true-positive cases, "
          f"{len(cases_should_pass)} true-negative cases, all correct.")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--selftest"]:
        return _selftest()

    # Default: the whole repository. T-001 widens this deliberately -- the
    # retired system scanned only its trading subdirectory.
    target = Path(argv[0]) if argv else Path(__file__).resolve().parents[1]
    if not target.exists():
        print(f"Target directory {target} does not exist -- nothing to scan.")
        return 0

    violations = scan_directory(target)
    if violations:
        print(f"EXECUTION BOUNDARY VIOLATION -- {len(violations)} finding(s):\n")
        for v in violations:
            print(v)
            print()
        print(
            "This system must never possess, reference, or invoke "
            "anything capable of placing, modifying, or cancelling a trade. "
            "If this is a genuine false positive (not execution capability), "
            "narrow or extend the pattern list in this script -- do not add "
            "a bypass comment."
        )
        return 1

    print(f"OK -- no execution-capability signatures found under {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
