"""The guards are security controls, so their own correctness is tested
rather than assumed. Each guard ships a fixture self-test; these run them
and add repository-wide scans."""
import subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _run(*args):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True)


def test_execution_boundary_selftest_passes():
    r = _run(str(SCRIPTS / "check_execution_boundary.py"), "--selftest")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFTEST PASSED" in r.stdout


def test_no_order_execution_capability_anywhere_in_the_repository():
    r = _run(str(SCRIPTS / "check_execution_boundary.py"), str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr


def test_execution_guard_catches_a_real_order_call():
    """A guard that never fires is indistinguishable from no guard."""
    sys.path.insert(0, str(SCRIPTS))
    from check_execution_boundary import scan_text
    assert scan_text("client.placeOrder(o)", Path("<t>"))
    assert scan_text("import tradovate", Path("<t>"))
    assert scan_text("def cancel_order(i): ...", Path("<t>"))


def test_execution_guard_permits_market_data():
    """Market-data connectivity is expected in T-002. If the guard blocked
    it, the ingestion ticket would be unwritable."""
    sys.path.insert(0, str(SCRIPTS))
    from check_execution_boundary import scan_text
    assert not scan_text("bars = md.get_bars('MNQ','1m')", Path("<t>"))
    assert not scan_text("# order book depth is market data", Path("<t>"))


def test_legacy_isolation_selftest_passes():
    r = _run(str(SCRIPTS / "check_no_legacy_dependency.py"), "--selftest")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFTEST PASSED" in r.stdout


def test_no_command_center_dependency_anywhere_in_the_repository():
    r = _run(str(SCRIPTS / "check_no_legacy_dependency.py"), str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr


def test_legacy_guard_catches_the_old_project_ref():
    sys.path.insert(0, str(SCRIPTS))
    from check_no_legacy_dependency import scan_text
    assert scan_text("url='https://ofzpaoxnnsenkritmnyu.supabase.co'", Path("<t>"))
    assert scan_text("from chief_of_staff import db", Path("<t>"))
    assert scan_text("select * from agent_runs", Path("<t>"))


def test_legacy_guard_requires_trading_prefixed_secret_names():
    sys.path.insert(0, str(SCRIPTS))
    from check_no_legacy_dependency import scan_text
    assert scan_text("TELEGRAM_BOT_TOKEN = x", Path("<t>"))
    assert not scan_text("TRADING_TELEGRAM_BOT_TOKEN = x", Path("<t>"))


def test_migration_ledger_parity_selftest_passes():
    r = _run(str(SCRIPTS / "check_migration_ledger_parity.py"), "--selftest")
    assert r.returncode == 0, r.stdout + r.stderr


def test_repository_migrations_are_wellformed():
    r = _run(str(SCRIPTS / "check_migration_ledger_parity.py"))
    assert r.returncode == 0, r.stdout + r.stderr
    # Local mode must say plainly it is not remote evidence.
    assert "LOCAL MODE" in r.stdout and "NOT evidence" in r.stdout


def test_ledger_parity_detects_command_center_style_drift():
    """The exact failure that went unnoticed in the retired system: same
    migration names, different version numbers."""
    sys.path.insert(0, str(SCRIPTS))
    from check_migration_ledger_parity import compare
    repo = [("20260906215558", "trading_foundation")]
    drifted = [("20260903011339", "trading_foundation")]
    problems = compare(repo, drifted)
    assert len(problems) == 2  # one missing remotely, one unexpected remotely


def test_credential_provisioning_selftest_passes():
    """The provisioning script is the deploy-time half of the two-tier
    role pattern. Its self-test asserts the thing most likely to drift:
    that its capability->env-var map still matches db.py's. If those
    disagree, a process gets a credential nothing reads."""
    r = _run(str(SCRIPTS / "provision_login_credentials.py"), "--selftest")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFTEST PASSED" in r.stdout


def test_provisioning_targets_proc_roles_never_group_roles():
    """Group `_svc` roles hold the grants and must never be able to log
    in. Only `_proc` roles receive credentials."""
    sys.path.insert(0, str(SCRIPTS))
    from provision_login_credentials import CAPABILITIES
    for role, _ in CAPABILITIES.values():
        assert role.endswith("_proc"), role
        assert not role.endswith("_svc"), role


def test_provisioning_keeps_analyst_and_reviewer_separate():
    sys.path.insert(0, str(SCRIPTS))
    from provision_login_credentials import CAPABILITIES
    assert CAPABILITIES["analyst"][0] != CAPABILITIES["reviewer"][0]
    assert CAPABILITIES["analyst"][1] != CAPABILITIES["reviewer"][1]


def test_provisioning_never_interpolates_a_password_into_sql():
    """Passwords are bound parameters, never formatted into the query
    text, so they cannot reach a server log."""
    import inspect
    sys.path.insert(0, str(SCRIPTS))
    import provision_login_credentials as prov
    src = inspect.getsource(prov.provision)
    assert "%s" in src, "password must be a bound parameter"
    assert "password}" not in src and "+ password" not in src


SOURCE_SUFFIXES = {".py", ".sql", ".yml", ".yaml", ".md", ".toml", ".ini", ".sh"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "venv", ".venv", "node_modules"}


def _source_files():
    for path in ROOT.rglob("*"):
        if path.suffix not in SOURCE_SUFFIXES or not path.is_file():
            continue
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        yield path.relative_to(ROOT)


def test_no_source_file_matches_an_ignore_rule():
    """`.gitignore` carries deliberately broad secret-hygiene patterns
    (`*credential*`, `*secret*`). Those match on the filename, not on the
    contents, so they will happily swallow a source file that merely
    talks about credentials -- and `git add -A` reports nothing when they
    do. That is exactly how `scripts/provision_login_credentials.py`
    missed the initial commit and only surfaced as a CI failure.

    `--no-index` makes git evaluate the rules regardless of whether the
    file is already tracked, so this catches a newly-added pattern that
    traps an existing file as well as a newly-added file that walks into
    an existing pattern."""
    trapped = []
    for rel in _source_files():
        r = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", str(rel)],
            cwd=ROOT, capture_output=True, text=True,
        )
        if r.returncode == 0:
            trapped.append(str(rel))
    assert not trapped, (
        "these source files match a .gitignore rule and would be dropped "
        "silently from a commit; add an explicit `!` negation for each: "
        + ", ".join(sorted(trapped))
    )


def test_every_script_the_guards_invoke_is_tracked_by_git():
    """A guard whose script is not in the repository is not a guard."""
    tracked = subprocess.run(
        ["git", "ls-files", "scripts/"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    for name in (
        "check_execution_boundary.py",
        "check_no_legacy_dependency.py",
        "check_migration_ledger_parity.py",
        "provision_login_credentials.py",
    ):
        assert f"scripts/{name}" in tracked, name


def test_provisioning_puts_the_project_ref_in_the_dsn_username():
    """Supavisor multiplexes projects behind one hostname and
    authenticates on `<role>.<project-ref>`. A bare role name parses as a
    valid DSN and then fails at connect time -- which would surface only
    at the deploy healthcheck, after seven live credentials had been
    minted. Asserted, not trusted."""
    sys.path.insert(0, str(SCRIPTS))
    from provision_login_credentials import build_dsn
    dsn = build_dsn("analyst_proc", "pw", "aws-0-x.pooler.supabase.com", "abc123")
    assert dsn.startswith("postgresql://analyst_proc.abc123:"), dsn
    # and still supports a direct endpoint, where the bare role is right
    assert build_dsn("analyst_proc", "pw", "db.x.supabase.co").startswith(
        "postgresql://analyst_proc:")


def test_provisioning_defaults_to_session_mode_not_transaction_mode():
    """Transaction mode (6543) cannot run `SET`. Every authority boundary
    in this system is role-scoped, so defaulting to a mode that forbids
    `SET ROLE` would be a trap for every stage built on this foundation."""
    sys.path.insert(0, str(SCRIPTS))
    import provision_login_credentials as prov
    assert prov.POOLER_PORT == prov.POOLER_PORT_SESSION == 5432
    assert prov.POOLER_PORT_TRANSACTION == 6543
    port = prov.build_dsn("r", "p", "h").rsplit(":", 1)[1].split("/")[0]
    assert port == "5432", port
