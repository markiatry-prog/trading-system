"""Which deliverable contract produced each session, from provenance.

THE PROBLEM. A continuous series such as NQ.c.0 is not one contract. It
points at whichever contract is front month, and at each quarterly roll
it steps to a different one trading at a different price level -- the
gap is carry and open interest migration, not anything the market did.

Every feature computed WITHIN a session is unaffected: VWAP, ATR, the
opening range and every structure break read one contiguous tape. The
damage is confined to features that reach BACK ACROSS a session
boundary. A prior-day high carried into the next session is a price the
CURRENT contract may never have traded at, so a "sweep" of it is an
arithmetic artifact of comparing two contracts, and the reversal it
predicts is a story told about a number that does not exist.

HOW ELIGIBILITY IS ESTABLISHED. From `Bar.contract_id`, which adapters
set from the provider's own symbology. NOT from a calendar. "The day
after the third Friday of a quarterly month" is a rule about a typical
year; it is wrong whenever a roll is early, late, staggered by product,
absent because the session was a holiday, or shifted because the vendor
rolls on volume rather than date. This module never asks what day it
is. It asks which contract printed the bars, and compares.

FAILING CLOSED. Anything short of a proven match is a refusal, with the
reason named: no prior session, a session whose bars carry no contract
provenance at all, a session spanning two contracts, or two sessions on
different contracts. None of these are "probably fine".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .records import EventType, FeatureType

# The primitives that read across a session boundary. Everything else in
# the engine is computed from the current session's tape alone and is
# unaffected by a roll, which is why the rule can exclude a hypothesis
# without excluding the session.
#
# Derived by inspection of FeatureEngine._roll_session: exactly the four
# levels it carries forward, plus the distances measured to them.
PRIOR_SESSION_FEATURES = frozenset({
    FeatureType.PRIOR_DAY_HIGH.value,
    FeatureType.PRIOR_DAY_LOW.value,
    FeatureType.OVERNIGHT_HIGH.value,
    FeatureType.OVERNIGHT_LOW.value,
    FeatureType.DISTANCE_TO_PRIOR_DAY_HIGH.value,
    FeatureType.DISTANCE_TO_PRIOR_DAY_LOW.value,
    FeatureType.DISTANCE_TO_OVERNIGHT_HIGH.value,
    FeatureType.DISTANCE_TO_OVERNIGHT_LOW.value,
})

# Events whose definition references one of those levels. Both sweep
# detectors fire only against prior-session references (see
# FeatureEngine._detect_sweeps), so the event type alone settles it.
PRIOR_SESSION_EVENTS = frozenset({
    EventType.LIQUIDITY_SWEEP_HIGH.value,
    EventType.LIQUIDITY_SWEEP_LOW.value,
})


class ContinuityVerdict(str, Enum):
    """Why a prior-session reference is or is not usable.

    Only SAME_CONTRACT is eligible. The other four are distinct because
    they call for different responses: a boundary is expected and
    harmless once excluded, missing provenance is a wiring defect, and a
    mixed session may mean the roll fell inside the trading day.
    """
    SAME_CONTRACT = "same_contract"
    CONTRACT_BOUNDARY = "contract_boundary"
    MIXED_SESSION = "mixed_session"
    NO_PRIOR_SESSION = "no_prior_session"
    UNKNOWN_PROVENANCE = "unknown_provenance"

    @property
    def eligible(self) -> bool:
        return self is ContinuityVerdict.SAME_CONTRACT


# The name this exclusion is recorded under, everywhere it is reported.
EXCLUSION_REASON = "contract_boundary_invalidation"


@dataclass(frozen=True)
class SessionContract:
    """Which contract, or contracts, printed one session's bars."""
    session_date: date
    contract_ids: Tuple[str, ...]        # sorted, deduplicated
    bars: int
    bars_without_provenance: int

    @property
    def is_mixed(self) -> bool:
        return len(self.contract_ids) > 1

    @property
    def has_provenance(self) -> bool:
        return bool(self.contract_ids) and self.bars_without_provenance == 0

    @property
    def sole_contract(self) -> Optional[str]:
        return self.contract_ids[0] if len(self.contract_ids) == 1 else None

    def as_row(self) -> dict:
        return {
            "session_date": self.session_date.isoformat(),
            "contract_ids": list(self.contract_ids),
            "bars": self.bars,
            "bars_without_provenance": self.bars_without_provenance,
        }


@dataclass(frozen=True)
class Continuity:
    """The verdict for one session, with everything needed to audit it."""
    session_date: date
    verdict: ContinuityVerdict
    prior_session_date: Optional[date] = None
    contract_id: Optional[str] = None
    prior_contract_id: Optional[str] = None

    @property
    def eligible(self) -> bool:
        return self.verdict.eligible

    def as_row(self) -> dict:
        return {
            "session_date": self.session_date.isoformat(),
            "verdict": self.verdict.value,
            "eligible": self.eligible,
            "prior_session_date": (self.prior_session_date.isoformat()
                                   if self.prior_session_date else None),
            "contract_id": self.contract_id,
            "prior_contract_id": self.prior_contract_id,
        }


class ContractTimeline:
    """Session -> contract, and the continuity verdict between neighbours.

    BUILD IT FROM THE SAME BARS THE ENGINE CONSUMES. "Prior session"
    means the previous session the engine actually saw, which after
    quality gating is the previous session that PASSED, not the previous
    calendar day. A timeline built from the ungated data would compare a
    different pair of sessions than the one whose level was carried
    forward, and would certify continuity across a gap the engine
    bridged.
    """

    def __init__(self, sessions: Mapping[date, SessionContract]):
        self._sessions: Dict[date, SessionContract] = dict(sessions)
        self._ordered: List[date] = sorted(self._sessions)
        self._index = {d: i for i, d in enumerate(self._ordered)}

    @classmethod
    def from_bars(cls, bars: Iterable, calendar) -> "ContractTimeline":
        """`calendar` supplies session_date_for, so the session boundary
        is the engine's, not midnight UTC."""
        seen: Dict[date, Dict[str, int]] = {}
        counts: Dict[date, int] = {}
        missing: Dict[date, int] = {}
        for bar in bars:
            day = calendar.session_date_for(bar.observed_at)
            counts[day] = counts.get(day, 0) + 1
            cid = getattr(bar, "contract_id", None)
            if cid is None:
                missing[day] = missing.get(day, 0) + 1
            else:
                seen.setdefault(day, {})[cid] = seen.setdefault(day, {}).get(cid, 0) + 1
        sessions = {
            day: SessionContract(
                session_date=day,
                contract_ids=tuple(sorted(seen.get(day, {}))),
                bars=counts[day],
                bars_without_provenance=missing.get(day, 0),
            )
            for day in counts
        }
        return cls(sessions)

    # -- queries -------------------------------------------------------

    def sessions(self) -> Sequence[date]:
        return tuple(self._ordered)

    def contract_on(self, day: date) -> Optional[SessionContract]:
        return self._sessions.get(day)

    def prior_session(self, day: date) -> Optional[date]:
        i = self._index.get(day)
        if i is None or i == 0:
            return None
        return self._ordered[i - 1]

    def continuity(self, day: date) -> Continuity:
        """The verdict for using a prior-session level in `day`."""
        current = self._sessions.get(day)
        if current is None or not current.has_provenance:
            return Continuity(day, ContinuityVerdict.UNKNOWN_PROVENANCE)
        if current.is_mixed:
            return Continuity(day, ContinuityVerdict.MIXED_SESSION,
                              contract_id=None)
        prior_day = self.prior_session(day)
        if prior_day is None:
            return Continuity(day, ContinuityVerdict.NO_PRIOR_SESSION,
                              contract_id=current.sole_contract)
        prior = self._sessions[prior_day]
        if not prior.has_provenance:
            return Continuity(day, ContinuityVerdict.UNKNOWN_PROVENANCE,
                              prior_session_date=prior_day,
                              contract_id=current.sole_contract)
        if prior.is_mixed:
            return Continuity(day, ContinuityVerdict.MIXED_SESSION,
                              prior_session_date=prior_day,
                              contract_id=current.sole_contract)
        if prior.sole_contract != current.sole_contract:
            return Continuity(day, ContinuityVerdict.CONTRACT_BOUNDARY,
                              prior_session_date=prior_day,
                              contract_id=current.sole_contract,
                              prior_contract_id=prior.sole_contract)
        return Continuity(day, ContinuityVerdict.SAME_CONTRACT,
                          prior_session_date=prior_day,
                          contract_id=current.sole_contract,
                          prior_contract_id=prior.sole_contract)

    def transitions(self) -> List[Continuity]:
        """Every session whose contract differs from its predecessor's.

        Over a five-year continuous front-month series this should be
        the number of quarterly rolls, give or take one at each end. A
        much larger number means the ids are not stable per contract and
        the whole rule is resting on sand -- which is exactly why this
        is reported rather than assumed.
        """
        return [c for c in (self.continuity(d) for d in self._ordered)
                if c.verdict is ContinuityVerdict.CONTRACT_BOUNDARY]

    def summary(self) -> dict:
        by_verdict: Dict[str, int] = {}
        for day in self._ordered:
            v = self.continuity(day).verdict.value
            by_verdict[v] = by_verdict.get(v, 0) + 1
        return {
            "sessions": len(self._ordered),
            "distinct_contracts": len({
                cid for s in self._sessions.values() for cid in s.contract_ids}),
            "verdicts": dict(sorted(by_verdict.items())),
            "transitions": [c.as_row() for c in self.transitions()],
        }
