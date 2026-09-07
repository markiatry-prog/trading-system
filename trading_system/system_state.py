"""The kill switch. Fail-closed, unconditionally.

THE INVARIANT, which every caller may rely on and none may weaken:

    If the runtime cannot POSITIVELY establish that operation is
    permitted, it behaves as paused.

"Cannot positively establish" is deliberately broad. A connection error,
a missing row, an unrecognized value, a timeout, a permission error, an
empty result, a driver exception -- every one of them is `paused`. A kill
switch that fails open is not a kill switch; it is a status indicator.

This is a rebuilt instance of a concept proven in the retired Command
Center, not a shared one. Nothing here connects to that system.

WHO MAY TURN IT BACK ON. Not this code, and not any component that uses
it. `trading.system_state` is writable only by trading_control_svc, which
runs no pipeline work and holds no stage grants. An application component
therefore cannot un-pause itself even if compromised -- the property the
whole mechanism exists for, enforced by database grants rather than by
this module's good behaviour.
"""
from __future__ import annotations

from enum import Enum


class SystemState(str, Enum):
    NORMAL = "normal"
    PAUSED = "paused"


class Paused(Exception):
    """Raised by require_normal when operation is not permitted."""


def check_system_state(reader) -> SystemState:
    """Never raises. `reader` is anything with a read_system_state()
    returning the raw state string; a real database handle in production,
    a fake in tests.

    Returns PAUSED for every outcome that is not an unambiguous 'normal'.
    """
    try:
        raw = reader.read_system_state()
    except Exception:
        return SystemState.PAUSED
    if raw is None:
        return SystemState.PAUSED
    try:
        return SystemState(raw)
    except (ValueError, TypeError):
        return SystemState.PAUSED


def require_normal(reader) -> SystemState:
    """The pre-execution gate. Call as the first action of any component
    that computes, calls out, or writes -- strictly before any side
    effect."""
    state = check_system_state(reader)
    if state is not SystemState.NORMAL:
        raise Paused("system_state is not confirmed 'normal' -- refusing to proceed")
    return state


def is_permitted(reader) -> bool:
    """Convenience for cooperative re-checks at safe boundaries inside a
    long-running operation, where raising would leave work half-done."""
    return check_system_state(reader) is SystemState.NORMAL
