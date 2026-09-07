"""The store is tested against a fake connection.

No database is required, and that is the point: these assert the SQL
shape and the transaction discipline, which is what would go wrong
silently. Whether the schema accepts the statements is asserted by the
pgTAP suite against a real Postgres.
"""
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_system.features.config import FeatureConfig  # noqa: E402
from trading_system.features.records import FeatureRecord, RecordKind  # noqa: E402
from trading_system.features.store import CAPABILITY, FeatureStore  # noqa: E402

T = datetime(2026, 1, 12, 15, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, owner): self.owner = owner
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.owner.statements.append((sql, params))
    def executemany(self, sql, rows): self.owner.batches.append((sql, list(rows)))
    def fetchone(self): return ("config-uuid",)


class FakeConn:
    def __init__(self, fail_on_write=False):
        self.statements, self.batches = [], []
        self.committed = self.rolled_back = self.closed = False
        self.fail_on_write = fail_on_write
    def cursor(self): return FakeCursor(self)
    def commit(self):
        if self.fail_on_write: raise RuntimeError("write failed")
        self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


def _record(**kw):
    base = dict(instrument_symbol="NQZ6", kind=RecordKind.FEATURE, type="vwap",
                effective_at=T, available_at=T, session_date="2026-01-12",
                value=Decimal("20000.25"))
    base.update(kw)
    return FeatureRecord(**base)


def _store(conn):
    return FeatureStore(lambda dsn, **kw: conn, "postgresql://x")


def test_writes_are_one_all_or_nothing_batch():
    """A partially written run looks identical to a complete one in any
    later query, and no field would reveal the truncation."""
    conn = FakeConn()
    n = _store(conn).write([_record(), _record(type="atr")], "run-1",
                           FeatureConfig(), "digest-1")
    assert n == 2
    assert len(conn.batches) == 1 and len(conn.batches[0][1]) == 2
    assert conn.committed and conn.closed and not conn.rolled_back


def test_a_failed_write_rolls_back_and_still_closes():
    conn = FakeConn(fail_on_write=True)
    with pytest.raises(RuntimeError):
        _store(conn).write([_record()], "run-1", FeatureConfig(), "d")
    assert conn.rolled_back and conn.closed and not conn.committed


def test_missing_credential_is_a_clear_refusal():
    store = FeatureStore(lambda dsn, **kw: FakeConn(), None)
    with pytest.raises(RuntimeError, match=CAPABILITY):
        store.write([_record()], "run-1", FeatureConfig(), "d")


def test_config_is_stated_once_and_referenced():
    conn = FakeConn()
    _store(conn).write([_record()], "run-1", FeatureConfig(), "d")
    inserts = [s for s, _ in conn.statements
               if "feature_configs" in s and s.strip().startswith("insert")]
    assert len(inserts) == 1, "the config must be inserted once, not per record"
    assert "on conflict (digest, engine_version) do nothing" in inserts[0]
    lookups = [s for s, _ in conn.statements
               if "feature_configs" in s and s.strip().startswith("select")]
    assert len(lookups) == 1, "one lookup to resolve the id"


def test_empty_batch_touches_nothing():
    conn = FakeConn()
    assert _store(conn).write([], "run-1", FeatureConfig(), "d") == 0
    assert not conn.statements and not conn.committed


def test_the_store_never_writes_outside_the_feature_tables():
    conn = FakeConn()
    _store(conn).write([_record()], "run-1", FeatureConfig(), "d")
    written = " ".join(s for s, _ in conn.statements) + " ".join(
        s for s, _ in conn.batches)
    for table in ("artifacts", "system_state", "runs", "model_versions"):
        assert f"into trading.{table}" not in written


def test_the_engine_itself_holds_no_database_handle():
    """The determinism tests are only meaningful if the engine has no
    state they cannot see."""
    import ast
    src = (ROOT / "trading_system" / "features" / "engine.py").read_text()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "psycopg2" not in imported
    assert "store" not in imported
    assert "db" not in imported
