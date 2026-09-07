"""Persistence for feature records.

DELIBERATELY SEPARATE FROM THE ENGINE. The engine holds no database
handle and cannot write anything; it is a pure function of the bars it
has seen. That is what makes the determinism tests meaningful -- an
engine that could write would have state the tests cannot see.

This module is the only thing that touches the feature store, and it
uses the feature_engine capability credential and no other, per the
two-tier role pattern. It cannot read a market observation or write an
analysis; the database refuses both.
"""
from __future__ import annotations

import json
from typing import Iterable, List, Optional, Sequence

from ..provenance import digest
from .config import FeatureConfig
from .engine import ENGINE_VERSION
from .records import FeatureRecord

CAPABILITY = "feature_engine"


class FeatureStore:
    """Writes configs and records. Takes a connection factory rather than
    a connection, so nothing here decides when to connect or holds one
    open across a run."""

    def __init__(self, connect, dsn: Optional[str]):
        self._connect = connect
        self._dsn = dsn

    def _require_dsn(self) -> str:
        if not self._dsn:
            raise RuntimeError(
                f"no DSN configured for the {CAPABILITY} capability; this "
                f"process was not given the credential it needs"
            )
        return self._dsn

    def ensure_config(self, conn, config: FeatureConfig) -> str:
        """Upsert-by-digest. The same parameter set is stated once."""
        params = json.dumps(config.as_dict(), sort_keys=True)
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into trading.feature_configs
                    (name, digest, engine_version, parameters)
                values (%s, %s, %s, %s::jsonb)
                on conflict (digest, engine_version) do nothing
                """,
                (config.name, config.digest(), ENGINE_VERSION, params),
            )
            cur.execute(
                "select id from trading.feature_configs "
                "where digest = %s and engine_version = %s",
                (config.digest(), ENGINE_VERSION),
            )
            row = cur.fetchone()
        if not row:
            raise RuntimeError("feature config was neither inserted nor found")
        return row[0]

    def write(self, records: Sequence[FeatureRecord], run_id: str,
              config: FeatureConfig, inputs_digest: str) -> int:
        """One transaction for the whole batch.

        All-or-nothing on purpose: a partially written run would look
        like a complete one to any later query, and there is no field
        that would reveal the truncation.
        """
        if not records:
            return 0
        conn = self._connect(self._require_dsn(), connect_timeout=10)
        try:
            config_id = self.ensure_config(conn, config)
            rows = [
                (
                    run_id, config_id, r.instrument_symbol, r.kind.value, r.type,
                    r.session_date, r.effective_at, r.available_at,
                    r.value, r.state, json.dumps(r.attributes, sort_keys=True),
                    inputs_digest,
                )
                for r in records
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    insert into trading.features
                        (run_id, config_id, instrument_symbol, kind, type,
                         session_date, effective_at, available_at, value,
                         state, attributes, inputs_digest)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    """,
                    rows,
                )
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
