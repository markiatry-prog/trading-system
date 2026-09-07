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
