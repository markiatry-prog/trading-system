"""Pre-registration of hypotheses.

THE PROBLEM THIS SOLVES. Without pre-registration, the natural workflow
is: run many conditionings, notice one that looks good, and write it up
as though it had been the question all along. Every statistic then
reported is wrong, because the multiple comparisons that produced the
finding are invisible. This is not misconduct; it is what happens by
default.

THE MECHANISM. The registry is APPEND-ONLY and HASH-CHAINED. Each entry
commits to the hash of the previous one, so inserting a hypothesis after
the fact -- or editing one to match what was found -- breaks the chain
and `verify()` says so. Results may only be attached to a hypothesis
that was registered BEFORE the results existed, and attaching to an
unregistered id raises.

WHAT A HYPOTHESIS MUST STATE UP FRONT. The event, the conditioning, the
horizon, the direction, and the PREDICTION -- including what would
falsify it. A hypothesis that cannot be falsified is not registered.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence

from ..provenance import digest, utcnow

GENESIS = "0" * 64


class Direction(str, Enum):
    """Which way the hypothesis says price should go.

    Declared in advance so 'favorable' is fixed before any outcome is
    seen. Choosing the direction afterwards doubles the effective number
    of tests while appearing to be one.
    """
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True)
class Hypothesis:
    id: str
    statement: str              # plain-language claim
    event_type: str             # the T-003 event it keys off
    direction: Direction
    horizon_minutes: int
    conditions: Dict[str, str]  # feature conditions, e.g. {"above_vwap": "above"}
    falsifier: str              # what result would REFUTE this
    registered_at: str
    previous_hash: str
    entry_hash: str = ""

    def compute_hash(self) -> str:
        body = {
            "id": self.id, "statement": self.statement,
            "event_type": self.event_type, "direction": self.direction.value,
            "horizon_minutes": self.horizon_minutes,
            "conditions": dict(sorted(self.conditions.items())),
            "falsifier": self.falsifier,
            "registered_at": self.registered_at,
            "previous_hash": self.previous_hash,
        }
        return hashlib.sha256(
            digest(body).encode("utf-8")).hexdigest()


class RegistryError(Exception):
    pass


class HypothesisRegistry:
    """Append-only, hash-chained. Sealed once testing begins."""

    def __init__(self) -> None:
        self._entries: List[Hypothesis] = []
        self._sealed = False

    @property
    def sealed(self) -> bool:
        return self._sealed

    def register(self, id: str, statement: str, event_type: str,
                 direction: Direction, horizon_minutes: int,
                 conditions: Optional[Dict[str, str]] = None,
                 falsifier: str = "") -> Hypothesis:
        if self._sealed:
            raise RegistryError(
                "the registry is sealed; hypotheses cannot be added after "
                "testing has begun. Adding one now would make every "
                "multiple-comparison correction understate the real count."
            )
        if any(h.id == id for h in self._entries):
            raise RegistryError(f"hypothesis {id!r} is already registered")
        if not falsifier or len(falsifier.strip()) < 15:
            raise RegistryError(
                f"{id!r} needs an explicit falsifier: what result would "
                f"refute it? A claim nothing could refute is not a hypothesis."
            )
        if horizon_minutes <= 0:
            raise RegistryError("horizon must be positive")
        prev = self._entries[-1].entry_hash if self._entries else GENESIS
        h = Hypothesis(
            id=id, statement=statement, event_type=event_type,
            direction=direction, horizon_minutes=horizon_minutes,
            conditions=dict(sorted((conditions or {}).items())),
            falsifier=falsifier.strip(),
            registered_at=utcnow().isoformat(), previous_hash=prev,
        )
        h = Hypothesis(**{**asdict(h), "direction": direction,
                          "entry_hash": h.compute_hash()})
        self._entries.append(h)
        return h

    def seal(self) -> None:
        """Called before the first test. After this the count is fixed."""
        self._sealed = True

    def get(self, id: str) -> Hypothesis:
        for h in self._entries:
            if h.id == id:
                return h
        raise RegistryError(
            f"{id!r} was never pre-registered. Results may only be reported "
            f"for hypotheses registered before the results existed."
        )

    def all(self) -> Sequence[Hypothesis]:
        return tuple(self._entries)

    def count(self) -> int:
        return len(self._entries)

    def verify(self) -> bool:
        """Recompute the chain. Any insertion, deletion or edit breaks it."""
        prev = GENESIS
        for h in self._entries:
            if h.previous_hash != prev:
                return False
            if h.compute_hash() != h.entry_hash:
                return False
            prev = h.entry_hash
        return True

    def provenance(self) -> dict:
        return {
            "count": len(self._entries),
            "sealed": self._sealed,
            "chain_valid": self.verify(),
            "head": self._entries[-1].entry_hash if self._entries else GENESIS,
            "hypotheses": [
                {"id": h.id, "event_type": h.event_type,
                 "direction": h.direction.value,
                 "horizon_minutes": h.horizon_minutes,
                 "conditions": h.conditions, "hash": h.entry_hash}
                for h in self._entries
            ],
        }
