"""Each capability reads its own DSN. There is deliberately no single
'application' credential -- one would collapse the boundaries the schema
exists to enforce."""
import pytest
from trading_system import db


def test_every_pipeline_stage_has_its_own_dsn_variable():
    assert set(db.DSN_VARS) == {
        "md_ingest", "feature_engine", "setup_detector",
        "analyst", "reviewer", "delivery", "trading_control",
    }


def test_analyst_and_reviewer_have_separate_credentials():
    """The adversarial separation must hold at the credential layer too --
    a shared DSN would make the database's stage policy pointless."""
    assert db.DSN_VARS["analyst"] != db.DSN_VARS["reviewer"]


def test_all_dsn_variables_are_trading_prefixed_and_unique():
    names = list(db.DSN_VARS.values())
    assert len(set(names)) == len(names)
    assert all(n.startswith("TRADING_") for n in names)


def test_unknown_capability_is_rejected():
    with pytest.raises(ValueError):
        db.dsn_for("superuser")


def test_state_reader_raises_without_a_dsn_rather_than_defaulting():
    """It must raise so check_system_state converts it to PAUSED. Silently
    returning 'normal' here would defeat the entire fail-closed design."""
    reader = db.StateReader(lambda *a, **k: None, None)
    with pytest.raises(RuntimeError):
        reader.read_system_state()


def test_state_reader_raises_when_no_row_exists():
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass
        def fetchone(self): return None
    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass
    reader = db.StateReader(lambda *a, **k: _Conn(), "dsn")
    with pytest.raises(RuntimeError):
        reader.read_system_state()


def test_state_reader_returns_the_state_and_closes_the_connection():
    closed = []
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass
        def fetchone(self): return ("normal",)
    class _Conn:
        def cursor(self): return _Cur()
        def close(self): closed.append(True)
    reader = db.StateReader(lambda *a, **k: _Conn(), "dsn")
    assert reader.read_system_state() == "normal"
    assert closed == [True]
