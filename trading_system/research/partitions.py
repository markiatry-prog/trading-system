"""Chronological discovery / validation / holdout separation.

WHY CHRONOLOGICAL AND NOT RANDOM. Random splits leak: adjacent minutes
are correlated, so a randomly held-out bar is all but present in
training. Market regimes also drift, and the only honest question is
whether something found in the past survives into a later period it
could not have influenced.

WHY THE HOLDOUT IS SEALED IN CODE. "Do not touch the holdout" is a
policy, and policies are followed until the result is disappointing.
Here it is a mechanism: the holdout raises on access unless explicitly
unsealed with a stated reason, and every unseal is recorded on the
partition itself. A study that quietly peeked cannot then claim it
did not -- the evidence travels with the object.

The seal is not a security control against a determined author; it is a
control against the far more common failure, which is a tired person
running one more query.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import List, Optional, Sequence, Tuple

from ..provenance import digest, utcnow


class Partition(str, Enum):
    DISCOVERY = "discovery"     # earliest; hypotheses may be formed here
    VALIDATION = "validation"   # middle; hypotheses are TESTED here
    HOLDOUT = "holdout"         # latest; opened once, at the very end


class HoldoutSealed(Exception):
    """Raised on any attempt to read the holdout while sealed."""


@dataclass
class UnsealRecord:
    at: str
    reason: str
    by: str


@dataclass
class ChronologicalPartitions:
    """Three contiguous, non-overlapping, ordered date ranges.

    Contiguity is enforced rather than assumed: a gap between discovery
    and validation would quietly discard data, and an overlap would let
    the same day inform both the hypothesis and its test.
    """
    discovery: Tuple[date, date]
    validation: Tuple[date, date]
    holdout: Tuple[date, date]
    _sealed: bool = True
    unseals: List[UnsealRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("discovery", "validation", "holdout"):
            start, end = getattr(self, name)
            if start > end:
                raise ValueError(f"{name} starts after it ends")
        if self.discovery[1] >= self.validation[0]:
            raise ValueError(
                "discovery overlaps validation: the same day would both "
                "form a hypothesis and test it"
            )
        if self.validation[1] >= self.holdout[0]:
            raise ValueError("validation overlaps holdout")

    @property
    def is_sealed(self) -> bool:
        return self._sealed

    def partition_for(self, day: date) -> Optional[Partition]:
        for name, rng in (("discovery", self.discovery),
                          ("validation", self.validation),
                          ("holdout", self.holdout)):
            if rng[0] <= day <= rng[1]:
                return Partition(name)
        return None

    def select(self, days: Sequence[date], partition: Partition) -> List[date]:
        """The only way to get days for a partition.

        Requesting the holdout while sealed raises. That is the point:
        the failure is loud and at the moment of the mistake, not
        discovered later in a review.
        """
        if partition is Partition.HOLDOUT and self._sealed:
            raise HoldoutSealed(
                "the final holdout is sealed. It may be opened ONCE, after "
                "hypotheses are fixed and validation is complete. If you are "
                "still developing or selecting, this exception is correct and "
                "the answer is not to unseal it."
            )
        return sorted(d for d in days if self.partition_for(d) is partition)

    def unseal(self, reason: str, by: str = "operator") -> None:
        if not reason or len(reason.strip()) < 20:
            raise ValueError(
                "unsealing requires a substantive stated reason; it is "
                "recorded permanently on the partition"
            )
        self._sealed = False
        self.unseals.append(UnsealRecord(at=utcnow().isoformat(), reason=reason.strip(), by=by))

    def reseal(self) -> None:
        self._sealed = True

    def provenance(self) -> dict:
        return {
            "discovery": [d.isoformat() for d in self.discovery],
            "validation": [d.isoformat() for d in self.validation],
            "holdout": [d.isoformat() for d in self.holdout],
            "holdout_ever_unsealed": bool(self.unseals),
            "unseals": [vars(u) for u in self.unseals],
        }

    def digest(self) -> str:
        return digest(self.provenance())


def split_chronologically(days: Sequence[date],
                          discovery_frac: float = 0.5,
                          validation_frac: float = 0.3) -> ChronologicalPartitions:
    """Split an ordered set of trading days by POSITION, not by calendar.

    Splitting by calendar length would give the partitions unequal
    numbers of sessions whenever holidays cluster, which quietly changes
    the statistical power of each stage.
    """
    ordered = sorted(set(days))
    if len(ordered) < 3:
        raise ValueError("need at least three trading days to partition")
    if not (0 < discovery_frac < 1 and 0 < validation_frac < 1):
        raise ValueError("fractions must be between 0 and 1")
    if discovery_frac + validation_frac >= 1:
        raise ValueError("discovery and validation leave nothing for holdout")
    n = len(ordered)
    d_end = max(1, int(n * discovery_frac))
    v_end = max(d_end + 1, int(n * (discovery_frac + validation_frac)))
    v_end = min(v_end, n - 1)
    return ChronologicalPartitions(
        discovery=(ordered[0], ordered[d_end - 1]),
        validation=(ordered[d_end], ordered[v_end - 1]),
        holdout=(ordered[v_end], ordered[-1]),
    )
