"""The contract-boundary research-validity rule.

THE RULE. An observation is eligible for a hypothesis that depends on a
prior-session reference level only when the prior-session level and the
current-session observation come from the SAME underlying futures
contract.

WHY IT IS A RESEARCH RULE AND NOT A DATA-QUALITY RULE. The data on
those sessions is not defective. Every print is real, the session is
complete, and every intraday feature computed from it is correct. What
is invalid is one specific comparison: this contract's price against
last contract's level. So the exclusion is scoped to the hypotheses
that make that comparison, and the session stays in the sample for
every other hypothesis. Excluding the whole session would throw away
sound observations of eleven other claims to fix a defect in two.

WHICH HYPOTHESES ARE AFFECTED IS DERIVED, NOT LISTED. Nothing here
names H6 or H7. A hypothesis is affected when its event type or any of
its conditioning features is one of the prior-session primitives
declared in `features.contracts`. A hypothesis added later that
conditions on distance-to-prior-day-high is caught automatically,
because the rule keys off what the hypothesis reads rather than off a
list somebody has to remember to update.

FAIL CLOSED. Absent provenance is not absent risk. A study given no
contract timeline cannot establish that ANY prior-session comparison is
within one contract, so none of them are eligible, and the report says
`unknown_provenance` rather than quietly reporting a full sample.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence

from ..features.contracts import (
    EXCLUSION_REASON, ContinuityVerdict, ContractTimeline,
    PRIOR_SESSION_EVENTS, PRIOR_SESSION_FEATURES,
)
from ..features.records import FeatureRecord
from .hypotheses import Hypothesis


def depends_on_prior_session_levels(hypothesis: Hypothesis) -> bool:
    """Does this hypothesis compare across a session boundary?"""
    if hypothesis.event_type in PRIOR_SESSION_EVENTS:
        return True
    return any(name in PRIOR_SESSION_FEATURES
               for name in hypothesis.conditions)


@dataclass
class EligibilityReport:
    """What the rule did to one hypothesis on one partition.

    Reported even when the rule does not apply, so the absence of an
    exclusion is a recorded finding rather than a silence that could
    equally mean the rule never ran.
    """
    rule: str = EXCLUSION_REASON
    applies: bool = False
    events_considered: int = 0
    events_eligible: int = 0
    events_excluded: int = 0
    excluded_by_verdict: Dict[str, int] = field(default_factory=dict)
    sessions_excluded: List[str] = field(default_factory=list)

    def as_row(self) -> dict:
        return {
            "rule": self.rule,
            "applies": self.applies,
            "events_considered": self.events_considered,
            "events_eligible": self.events_eligible,
            "events_excluded": self.events_excluded,
            "excluded_by_verdict": dict(sorted(self.excluded_by_verdict.items())),
            "sessions_excluded": sorted(self.sessions_excluded),
        }


def filter_eligible(hypothesis: Hypothesis,
                    events: Sequence[FeatureRecord],
                    timeline: Optional[ContractTimeline]):
    """Split a hypothesis's candidate events into eligible and refused.

    Returns `(eligible_events, EligibilityReport)`. For a hypothesis
    that does not reach across a session boundary the events are
    returned untouched and `applies` is False -- the rule has no opinion
    about an opening-range break, and pretending otherwise would shrink
    a sound sample.
    """
    report = EligibilityReport(applies=depends_on_prior_session_levels(hypothesis),
                               events_considered=len(events))
    if not report.applies:
        report.events_eligible = len(events)
        return list(events), report

    eligible: List[FeatureRecord] = []
    excluded_sessions = set()
    for event in events:
        verdict = _verdict_for(event, timeline)
        if verdict.eligible:
            eligible.append(event)
            continue
        report.events_excluded += 1
        key = verdict.value
        report.excluded_by_verdict[key] = report.excluded_by_verdict.get(key, 0) + 1
        excluded_sessions.add(event.session_date)
    report.events_eligible = len(eligible)
    report.sessions_excluded = sorted(excluded_sessions)
    return eligible, report


def _verdict_for(event: FeatureRecord,
                 timeline: Optional[ContractTimeline]) -> ContinuityVerdict:
    if timeline is None:
        return ContinuityVerdict.UNKNOWN_PROVENANCE
    try:
        session = date.fromisoformat(event.session_date)
    except ValueError:
        return ContinuityVerdict.UNKNOWN_PROVENANCE
    return timeline.continuity(session).verdict
