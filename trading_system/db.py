"""Database access. Thin by design: T-001 has no pipeline to serve, so
this is the connection contract and the kill-switch read, nothing more.

Each capability identity gets its OWN DSN. There is no single
"application" credential, and adding one would collapse the boundaries
the schema exists to enforce.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

from . import env

# One variable per capability. A component reads only its own.
DSN_VARS = {
    "md_ingest": "TRADING_DATABASE_URL_MD_INGEST",
    "feature_engine": "TRADING_DATABASE_URL_FEATURE_ENGINE",
    "setup_detector": "TRADING_DATABASE_URL_SETUP_DETECTOR",
    "analyst": "TRADING_DATABASE_URL_ANALYST",
    "reviewer": "TRADING_DATABASE_URL_REVIEWER",
    "delivery": "TRADING_DATABASE_URL_DELIVERY",
    "trading_control": "TRADING_DATABASE_URL_CONTROL",
}


def dsn_for(capability: str) -> Optional[str]:
    try:
        var = DSN_VARS[capability]
    except KeyError:
        raise ValueError(f"unknown capability: {capability!r}") from None
    return env.get(var)


class StateReader:
    """Reads the kill switch over a live connection.

    Deliberately raises rather than returning a default on any failure:
    system_state.check_system_state() catches everything and converts it
    to PAUSED. Swallowing an error here would turn an unreachable database
    into a silent 'normal', which is the exact failure mode the whole
    fail-closed design exists to prevent.
    """

    def __init__(self, connect, dsn: Optional[str]):
        self._connect = connect
        self._dsn = dsn

    def read_system_state(self) -> str:
        if not self._dsn:
            raise RuntimeError("no DSN configured for this capability")
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select state from trading.system_state")
                row = cur.fetchone()
        if not row:
            raise RuntimeError("system_state has no row")
        return row[0]

    @contextmanager
    def _connection(self):
        conn = self._connect(self._dsn, connect_timeout=10)
        try:
            yield conn
        finally:
            conn.close()
