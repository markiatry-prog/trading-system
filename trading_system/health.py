"""Minimal health surface. T-001's deployable proves the isolated stack
runs and can reach its own database -- nothing more. No ingestion, no
analysis, no scheduling; those are later tickets.

The health report deliberately distinguishes three things a single
boolean would conflate:

    reachable   can we talk to our database at all?
    permitted   is the kill switch letting work proceed?
    ready       both of the above

A paused system is HEALTHY. It is doing exactly what it was told to do.
Reporting `paused` as unhealthy would make an operator's deliberate stop
look like an outage and invite someone to "fix" it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from .system_state import SystemState, check_system_state


@dataclass(frozen=True)
class Health:
    service: str
    reachable: bool
    permitted: bool
    state: str
    detail: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.reachable and self.permitted

    def as_dict(self) -> dict:
        d = asdict(self)
        d["ready"] = self.ready
        return d


SERVICE_NAME = "trading-system"


def report(reader) -> Health:
    """Never raises. A health endpoint that can fail is not a health
    endpoint -- it turns a degraded dependency into a crashed service."""
    state = check_system_state(reader)
    reachable = True
    detail = None
    try:
        reader.read_system_state()
    except Exception as exc:
        reachable = False
        # Class name only: a DSN or credential can appear in a driver's
        # exception message, and this value is served over HTTP.
        detail = type(exc).__name__
    return Health(
        service=SERVICE_NAME,
        reachable=reachable,
        permitted=state is SystemState.NORMAL,
        state=state.value,
        detail=detail,
    )
