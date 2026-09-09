"""Matched controls: what the market did anyway.

THE QUESTION THIS CHANGES. Testing a post-event return against ZERO
asks "did price move after this event?". Over 2021-2026 NQ trended
upward, so the answer is partly yes for any upward-facing claim and
partly no for any downward-facing one, whatever the event means. Add a
few thousand observations and that secular drift alone can push a
confidence interval off zero.

The question that survives contact with a trending market is:

    does this event change the forward distribution relative to a
    COMPARABLE NON-EVENT state?

So every hypothesis is now measured against a control arm: forward
paths anchored at moments that resemble the event's moments in
everything except the event.

WHAT IS MATCHED, AND WHY EACH

  instrument          the control is drawn from the same series; a
                      different contract has a different tick and level
  time of day         intraday seasonality is enormous. The first
                      thirty minutes after the RTH open and the middle
                      of the overnight session are not the same market,
                      and events cluster in the former
  session eligibility only sessions that passed the quality gate, and
                      for prior-session hypotheses only those whose
                      contract provenance is eligible -- the same
                      constraints the event arm is held to
  volatility regime   a 40-point move means something different in a
                      quiet week than a violent one. Measured as the
                      ATR knowable AT the anchor over the median of the
                      previous sessions' closing ATR, so the comparison
                      is to the market's own recent past
  forward horizon     identical by construction

WHAT IS DELIBERATELY NOT MATCHED. Anything that is part of the signal.
Matching an opening-range-break control on "price is above the opening
range high" would select controls that are themselves breakouts, and
the lift would be driven to zero by construction -- a perfect way to
prove nothing while looking rigorous.

NO LOOKAHEAD. The volatility reference is the median of COMPLETED
prior sessions, which is fixed before the current session opens. The
ATR at the anchor comes through the same `available_at` filter the
study uses everywhere. The stratum of an anchor could have been
computed live, at that anchor, with no knowledge of anything after it.

STRATIFIED REWEIGHTING, NOT SAMPLING. The control mean is a weighted
average of per-stratum control means, weighted by how the EVENTS are
distributed across strata. That is the direct-standardised estimate:
what the market would have done at these times and in these regimes if
the event had not been the reason for looking. It is deterministic --
no matched-pair draw, so no sampling noise in the point estimate.

INFERENCE WITHOUT A GAUSSIAN. One-minute forward returns are heavy
tailed and skewed. The interval is a percentile bootstrap over events
and controls; the p-value is a stratified permutation test that
relabels event and control within each stratum. Neither assumes a
distribution.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from statistics import median
from typing import (Dict, Iterable, List, Mapping, Optional, Sequence, Set,
                    Tuple)

from ..features.records import FeatureRecord, FeatureType
from ..market_data import Bar
from ..provenance import digest
from .hypotheses import Direction, Hypothesis
from .outcomes import BarWindowIndex, ForwardPath, measure_forward_path

BASELINE_VERSION = "1.0.0"

UNKNOWN_BAND = "unknown"


class BaselineError(Exception):
    pass


@dataclass(frozen=True)
class MatchingSpec:
    """Every knob, declared in one place so it enters the research identity.

    A matching rule chosen after seeing which rule produced a nicer lift
    would be the same data-snooping this framework exists to prevent, one
    level down. These values are fixed before the comparison runs and are
    hashed into the analysis's identity.
    """
    # Time-of-day bin width in minutes, measured from the RTH open.
    session_minute_bucket: int = 30
    # Ratio edges for the volatility bands: below the first is "quiet",
    # above the last is "violent", between them "normal".
    volatility_band_edges: Tuple[str, ...] = ("0.75", "1.25")
    # Completed sessions used for the volatility reference.
    volatility_lookback_sessions: int = 20
    # A stratum with fewer controls than this cannot support a
    # comparison; its events are dropped from the lift and REPORTED.
    min_controls_per_stratum: int = 20
    # The conditioning contrast draws its comparison arm from events of
    # the same type rather than from the whole session, so it is
    # inherently an order of magnitude smaller and needs its own floor.
    # Lower, but still declared in advance rather than tuned to whatever
    # made a result appear.
    min_conditioning_per_stratum: int = 10
    # Cap per stratum, reservoir-sampled, so the control arm stays
    # bounded on 1.7M bars without biasing toward early dates. 200 is
    # ample for a stratum mean -- its standard error is already far
    # below the event arm's, which has orders of magnitude fewer
    # observations -- and the resampling cost is linear in this number.
    max_controls_per_stratum: int = 200
    control_sample_seed: int = 20260909
    bootstrap_iterations: int = 2000
    bootstrap_seed: int = 20260909
    bootstrap_confidence: float = 0.95
    permutation_iterations: int = 2000
    permutation_seed: int = 20260909
    # Inference resamples SESSIONS. Below this many, a percentile
    # interval over clusters is not meaningful and none is reported --
    # a wide interval is honest, a fabricated one is not.
    min_sessions_for_inference: int = 10
    # Symmetric threshold for the "+X before -Y" question, in multiples
    # of the ATR knowable at the anchor -- so it means the same thing in
    # a quiet week and a violent one.
    hit_first_atr_multiple: str = "1.0"

    def digest(self) -> str:
        return digest({"baseline_version": BASELINE_VERSION,
                       **{k: str(v) for k, v in sorted(asdict(self).items())}})


@dataclass(frozen=True)
class Stratum:
    """The matched cell an anchor falls in."""
    time_bucket: int          # whole buckets since the RTH open; may be < 0
    volatility_band: str

    def as_key(self) -> Tuple[int, str]:
        return (self.time_bucket, self.volatility_band)


@dataclass(frozen=True)
class Anchor:
    """A moment at which a forward path is measured."""
    at: datetime
    session_date: date
    stratum: Stratum
    atr: Optional[Decimal]


class VolatilityRegime:
    """ATR at the anchor, relative to the market's own recent past.

    The reference is the median closing ATR of the previous N COMPLETED
    sessions. Completed is the operative word: that median is fixed
    before the current session opens, so nothing about the current
    session -- least of all its outcome -- can influence which band its
    anchors land in.
    """

    def __init__(self, spec: MatchingSpec):
        self.spec = spec
        self._edges = [Decimal(e) for e in spec.volatility_band_edges]
        self._session_atr: Dict[date, Decimal] = {}
        self._ordered: List[date] = []

    def observe_session(self, session_date: date,
                        records: Iterable[FeatureRecord]) -> None:
        """Record one completed session's closing ATR."""
        last = None
        for r in records:
            if r.type == FeatureType.ATR.value and r.value is not None:
                last = r.value
        if last is not None:
            if session_date not in self._session_atr:
                self._ordered.append(session_date)
            self._session_atr[session_date] = last

    def reference_for(self, session_date: date) -> Optional[Decimal]:
        prior = [d for d in self._ordered if d < session_date]
        if len(prior) < self.spec.volatility_lookback_sessions:
            return None
        window = prior[-self.spec.volatility_lookback_sessions:]
        values = [self._session_atr[d] for d in window]
        return median(values)

    def band(self, atr: Optional[Decimal],
             reference: Optional[Decimal]) -> str:
        if atr is None or reference is None or reference == 0:
            return UNKNOWN_BAND
        ratio = Decimal(atr) / Decimal(reference)
        for i, edge in enumerate(self._edges):
            if ratio < edge:
                return f"band{i}"
        return f"band{len(self._edges)}"


def time_bucket(at: datetime, rth_open: datetime, width_minutes: int) -> int:
    """Whole buckets since the RTH open. Negative before it."""
    delta = (at - rth_open).total_seconds() / 60.0
    return int(delta // width_minutes)


@dataclass
class ArmStats:
    """One arm of the comparison, summarised."""
    n: int = 0
    mean_signed_return: Optional[float] = None
    mean_mfe: Optional[float] = None
    mean_mae: Optional[float] = None
    favorable_first_fraction: Optional[float] = None
    hit_first_favorable_fraction: Optional[float] = None

    def as_row(self) -> dict:
        return {"n": self.n,
                "mean_signed_return": self.mean_signed_return,
                "mean_mfe": self.mean_mfe, "mean_mae": self.mean_mae,
                "favorable_first_fraction": self.favorable_first_fraction,
                "hit_first_favorable_fraction":
                    self.hit_first_favorable_fraction}


@dataclass
class Observation:
    """One measured forward path, reduced to what the comparison needs.

    `session_date` is the CLUSTER. Events inside one session share a
    regime, a news cycle and often overlapping forward windows, so they
    are nothing like independent draws; inference resamples sessions,
    not observations, and needs to know which session each came from.
    """
    stratum_key: Tuple[int, str]
    session_date: str
    signed_return: float
    mfe: float
    mae: float
    favorable_first: Optional[bool]
    hit_first_favorable: Optional[bool]


@dataclass
class BaselineComparison:
    """Event arm against matched control arm, with the lift between them."""
    hypothesis_id: str
    partition: str
    horizon_minutes: int
    direction: str
    event: ArmStats = field(default_factory=ArmStats)
    control: ArmStats = field(default_factory=ArmStats)
    # The ACTUAL comparator. `control` summarises every control anchor
    # taken, which spans the whole day; this is that pool reweighted to
    # the strata the events occupy. The raw control mean is diagnostic
    # only -- reading it as the baseline would compare an event that
    # fires at the open against the average moment of the night.
    control_standardised_mean: Optional[float] = None
    lift: Optional[float] = None
    # THE INFERENCE OF RECORD: sessions resampled as clusters.
    lift_ci_low: Optional[float] = None
    lift_ci_high: Optional[float] = None
    lift_p_value: Optional[float] = None
    inference_unit: str = "session_cluster"
    unique_sessions: int = 0
    effective_clusters: Optional[float] = None
    # The event-level figures, kept only so the difference is visible.
    # They treat every event as an independent draw, which events inside
    # one session are not, so they are narrower than the evidence
    # supports and must never be quoted as the result.
    event_level_ci_low: Optional[float] = None
    event_level_ci_high: Optional[float] = None
    event_level_p_value: Optional[float] = None
    mfe_lift: Optional[float] = None
    mae_lift: Optional[float] = None
    favorable_first_lift: Optional[float] = None
    hit_first_lift: Optional[float] = None
    strata_used: int = 0
    strata_dropped: int = 0
    events_dropped_for_thin_strata: int = 0
    spec_digest: str = ""
    note: str = ""

    @property
    def clustering_changes_the_conclusion(self) -> Optional[bool]:
        """Did treating events as independent flip the answer?

        The number the operator asked for: whether event-level and
        session-clustered inference materially differ.
        """
        clustered = self.lift_excludes_zero
        if clustered is None or self.event_level_ci_low is None:
            return None
        naive = (self.event_level_ci_low > 0) or (self.event_level_ci_high < 0)
        return naive != clustered

    @property
    def lift_excludes_zero(self) -> Optional[bool]:
        if self.lift_ci_low is None or self.lift_ci_high is None:
            return None
        return (self.lift_ci_low > 0) or (self.lift_ci_high < 0)

    def as_row(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id, "partition": self.partition,
            "horizon_minutes": self.horizon_minutes, "direction": self.direction,
            "event": self.event.as_row(), "control": self.control.as_row(),
            "control_standardised_mean": self.control_standardised_mean,
            "absolute_effect": self.event.mean_signed_return,
            "lift": self.lift,
            "lift_ci_low": self.lift_ci_low, "lift_ci_high": self.lift_ci_high,
            "lift_ci_excludes_zero": self.lift_excludes_zero,
            "lift_p_value": self.lift_p_value,
            "inference_unit": self.inference_unit,
            "unique_sessions": self.unique_sessions,
            "effective_clusters": self.effective_clusters,
            "event_level_ci_low": self.event_level_ci_low,
            "event_level_ci_high": self.event_level_ci_high,
            "event_level_p_value": self.event_level_p_value,
            "clustering_changes_the_conclusion":
                self.clustering_changes_the_conclusion,
            "mfe_lift": self.mfe_lift, "mae_lift": self.mae_lift,
            "favorable_first_lift": self.favorable_first_lift,
            "hit_first_lift": self.hit_first_lift,
            "strata_used": self.strata_used,
            "strata_dropped": self.strata_dropped,
            "events_dropped_for_thin_strata":
                self.events_dropped_for_thin_strata,
            "baseline_version": BASELINE_VERSION,
            "matching_spec_digest": self.spec_digest,
            "note": self.note,
        }


# --- the estimator ----------------------------------------------------

def _mean(values: Sequence[float]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None


def _fraction_true(values: Sequence[Optional[bool]]) -> Optional[float]:
    seen = [v for v in values if v is not None]
    return (sum(1 for v in seen if v) / len(seen)) if seen else None


def _summarise(observations: Sequence[Observation]) -> ArmStats:
    return ArmStats(
        n=len(observations),
        mean_signed_return=_mean([o.signed_return for o in observations]),
        mean_mfe=_mean([o.mfe for o in observations]),
        mean_mae=_mean([o.mae for o in observations]),
        favorable_first_fraction=_fraction_true(
            [o.favorable_first for o in observations]),
        hit_first_favorable_fraction=_fraction_true(
            [o.hit_first_favorable for o in observations]),
    )


def _by_stratum(observations: Sequence[Observation]
                ) -> Dict[Tuple[int, str], List[Observation]]:
    out: Dict[Tuple[int, str], List[Observation]] = {}
    for o in observations:
        out.setdefault(o.stratum_key, []).append(o)
    return out


def _standardised(control_by_stratum: Mapping[Tuple[int, str], List[Observation]],
                  weights: Mapping[Tuple[int, str], float],
                  pick) -> Optional[float]:
    """Direct standardisation: per-stratum control mean, weighted by how
    the EVENTS are distributed across strata."""
    total = 0.0
    used = 0.0
    for key, weight in weights.items():
        pool = control_by_stratum.get(key)
        if not pool:
            continue
        values = [v for v in (pick(o) for o in pool) if v is not None]
        if not values:
            continue
        total += weight * (sum(values) / len(values))
        used += weight
    if used == 0:
        return None
    return total / used          # renormalised over the strata that exist


def _signed(o: Observation) -> float:
    return o.signed_return


def compare_to_baseline(hypothesis: Hypothesis, partition: str,
                        event_observations: Sequence[Observation],
                        control_observations: Sequence[Observation],
                        spec: MatchingSpec = MatchingSpec()
                        ) -> BaselineComparison:
    """Event arm against matched control arm.

    Events whose stratum has too few controls are DROPPED from the lift
    and counted, because a lift computed against three control
    observations is a number with no content. They remain in the event
    arm's own summary, so the two sample sizes can disagree and the
    reader can see by how much.
    """
    result = BaselineComparison(
        hypothesis_id=hypothesis.id, partition=partition,
        horizon_minutes=hypothesis.horizon_minutes,
        direction=hypothesis.direction.value,
        spec_digest=spec.digest())
    result.event = _summarise(event_observations)
    result.control = _summarise(control_observations)
    if not event_observations:
        result.note = "no events"
        return result

    controls = _by_stratum(control_observations)
    events = _by_stratum(event_observations)
    usable = {k: v for k, v in controls.items()
              if len(v) >= spec.min_controls_per_stratum}
    kept_events = {k: v for k, v in events.items() if k in usable}
    result.strata_used = len(kept_events)
    result.strata_dropped = len(events) - len(kept_events)
    result.events_dropped_for_thin_strata = sum(
        len(v) for k, v in events.items() if k not in usable)
    if not kept_events:
        result.note = ("no stratum had enough matched controls; a lift "
                       "cannot be computed for this hypothesis")
        return result

    total_events = sum(len(v) for v in kept_events.values())
    weights = {k: len(v) / total_events for k, v in kept_events.items()}
    matched_events = [o for v in kept_events.values() for o in v]

    event_mean = _mean([o.signed_return for o in matched_events])
    control_mean = _standardised(usable, weights, _signed)
    if event_mean is None or control_mean is None:
        result.note = "no measurable returns in the matched strata"
        return result
    result.control_standardised_mean = control_mean
    result.lift = event_mean - control_mean

    for attr, pick in (("mfe_lift", lambda o: o.mfe),
                       ("mae_lift", lambda o: o.mae),
                       ("favorable_first_lift",
                        lambda o: None if o.favorable_first is None
                        else float(o.favorable_first)),
                       ("hit_first_lift",
                        lambda o: None if o.hit_first_favorable is None
                        else float(o.hit_first_favorable))):
        arm = [v for v in (pick(o) for o in matched_events) if v is not None]
        base = _standardised(usable, weights, pick)
        if arm and base is not None:
            setattr(result, attr, (sum(arm) / len(arm)) - base)

    # THE INFERENCE OF RECORD. Sessions are the unit: see
    # `cluster_bootstrap` for why events inside one are not independent.
    matched_controls = [o for k, v in usable.items() if k in kept_events
                        for o in v]
    low, high, p, sessions = cluster_bootstrap(matched_events,
                                               matched_controls, spec)
    result.lift_ci_low, result.lift_ci_high, result.lift_p_value = low, high, p
    result.unique_sessions = sessions
    result.effective_clusters = effective_clusters(matched_events)
    if low is None:
        result.note = (
            f"{sessions} sessions support this estimate, below the "
            f"{spec.min_sessions_for_inference} needed to resample clusters; "
            f"no interval is reported rather than a falsely narrow one."
            + (f" {result.note}" if result.note else ""))

    # Event-level, for comparison ONLY. Reported so the cost of the old
    # IID assumption is visible instead of merely asserted.
    naive_low, naive_high = _bootstrap_lift_interval(kept_events, usable, spec)
    result.event_level_ci_low, result.event_level_ci_high = naive_low, naive_high
    result.event_level_p_value = _permutation_p_value(
        kept_events, usable, result.lift, spec)
    return result


def _values_by_stratum(by_stratum) -> Dict[Tuple[int, str], List[float]]:
    """Signed returns as plain floats, once.

    The resampling loops below run thousands of times over every
    observation in both arms. Reaching through `Observation` attributes
    inside them costs more than the arithmetic does, and on the real
    sample it was the difference between a minute and an hour.
    """
    return {k: [o.signed_return for o in v] for k, v in by_stratum.items()}


def _lift_from_values(event_values: Mapping[Tuple[int, str], List[float]],
                      control_values: Mapping[Tuple[int, str], List[float]]
                      ) -> Optional[float]:
    total = sum(len(v) for v in event_values.values())
    if not total:
        return None
    event_sum = 0.0
    control_total = 0.0
    weight_used = 0.0
    for key, values in event_values.items():
        event_sum += sum(values)
        controls = control_values.get(key)
        if not controls:
            continue
        weight = len(values) / total
        control_total += weight * (sum(controls) / len(controls))
        weight_used += weight
    if weight_used == 0:
        return None
    return (event_sum / total) - (control_total / weight_used)


def cluster_contributions(observations: Sequence[Observation]
                          ) -> Dict[str, Dict[Tuple[int, str], Tuple[float, int]]]:
    """Per session, per stratum: (sum of signed returns, count).

    The stratified lift is a function of per-stratum sums and counts
    only -- see `_lift_from_values` -- so a session's entire
    contribution collapses to a handful of numbers. A bootstrap
    iteration is then addition rather than list building, which is what
    makes resampling 1,234 sessions two thousand times affordable.
    """
    out: Dict[str, Dict[Tuple[int, str], Tuple[float, int]]] = {}
    for o in observations:
        per_stratum = out.setdefault(o.session_date, {})
        total, count = per_stratum.get(o.stratum_key, (0.0, 0))
        per_stratum[o.stratum_key] = (total + o.signed_return, count + 1)
    return out


def effective_clusters(observations: Sequence[Observation]) -> Optional[float]:
    """Kish effective sample size over sessions.

    (sum n_s)^2 / sum n_s^2. Equal to the session count when every
    session contributes equally, and collapsing toward 1 when one
    session contributes most of the observations -- which is exactly the
    situation where a raw event count overstates the evidence. 400
    events from three sessions are not 400 confirmations.
    """
    per_session: Dict[str, int] = {}
    for o in observations:
        per_session[o.session_date] = per_session.get(o.session_date, 0) + 1
    counts = list(per_session.values())
    if not counts:
        return None
    total = sum(counts)
    return (total * total) / sum(c * c for c in counts)


def _lift_from_totals(event_totals, control_totals) -> Optional[float]:
    """The same estimator as `_lift_from_values`, from sums and counts."""
    total = sum(n for _s, n in event_totals.values())
    if not total:
        return None
    event_sum = sum(s for s, _n in event_totals.values())
    control_total = 0.0
    weight_used = 0.0
    for key, (_esum, ecount) in event_totals.items():
        control = control_totals.get(key)
        if not control or control[1] == 0:
            continue
        weight = ecount / total
        control_total += weight * (control[0] / control[1])
        weight_used += weight
    if weight_used == 0:
        return None
    return (event_sum / total) - (control_total / weight_used)


def cluster_bootstrap(event_observations: Sequence[Observation],
                      control_observations: Sequence[Observation],
                      spec: MatchingSpec):
    """Percentile bootstrap resampling SESSIONS, not observations.

    THE PROBLEM WITH RESAMPLING OBSERVATIONS. Events inside one session
    share a volatility regime, a news cycle, and frequently overlapping
    forward windows -- two ORB breaks twenty minutes apart are largely
    the same forty minutes of tape. Resampling them independently treats
    each as fresh evidence, so a study drawing 400 events from three
    unusual sessions reports the interval of 400 independent
    observations. It is not merely optimistic; it is the mechanism by
    which one strange week becomes a discovery.

    Sessions are drawn WITH REPLACEMENT and every eligible observation
    belonging to a drawn session comes with it, in both arms and across
    every stratum. A session drawn twice contributes twice; a session
    not drawn contributes nothing. The matching strata are preserved
    inside each resample -- the lift is recomputed by the same
    standardisation, so a resample that happens to omit a stratum
    renormalises over the rest rather than silently comparing unlike
    things.

    Both arms are drawn from the SAME sampled sessions, because a
    session's controls and its events share whatever made that session
    unusual, and breaking that pairing would put some of the dependence
    back.

    Returns (ci_low, ci_high, two_sided_p, n_sessions).
    """
    events = cluster_contributions(event_observations)
    controls = cluster_contributions(control_observations)
    sessions = sorted(set(events) | set(controls))
    if len(sessions) < spec.min_sessions_for_inference:
        return None, None, None, len(sessions)

    rng = random.Random(spec.bootstrap_seed)
    choices = rng.choices
    lifts: List[float] = []
    for _ in range(spec.bootstrap_iterations):
        event_totals: Dict[Tuple[int, str], Tuple[float, int]] = {}
        control_totals: Dict[Tuple[int, str], Tuple[float, int]] = {}
        for session in choices(sessions, k=len(sessions)):
            for key, (value, count) in events.get(session, {}).items():
                have = event_totals.get(key)
                event_totals[key] = ((have[0] + value, have[1] + count)
                                     if have else (value, count))
            for key, (value, count) in controls.get(session, {}).items():
                have = control_totals.get(key)
                control_totals[key] = ((have[0] + value, have[1] + count)
                                       if have else (value, count))
        lift = _lift_from_totals(event_totals, control_totals)
        if lift is not None:
            lifts.append(lift)
    if len(lifts) < 2:
        return None, None, None, len(sessions)

    lifts.sort()
    tail = (1.0 - spec.bootstrap_confidence) / 2.0
    low = lifts[max(0, int(tail * len(lifts)))]
    high = lifts[min(len(lifts) - 1, int((1.0 - tail) * len(lifts)))]
    # Two-sided percentile p: how much of the resampled distribution sits
    # on the far side of zero. Add-one, because a finite number of
    # resamples cannot support a p-value of exactly zero.
    below = sum(1 for v in lifts if v <= 0)
    above = sum(1 for v in lifts if v >= 0)
    p = min(1.0, 2.0 * (min(below, above) + 1) / (len(lifts) + 1))
    return low, high, p, len(sessions)


def _bootstrap_lift_interval(events_by_stratum, controls_by_stratum,
                             spec: MatchingSpec):
    """Percentile bootstrap over BOTH arms.

    Resampling only the events would treat the control mean as known
    exactly, which it is not, and would report an interval narrower than
    the evidence supports.
    """
    rng = random.Random(spec.bootstrap_seed)
    events = _values_by_stratum(events_by_stratum)
    controls = {k: v for k, v in _values_by_stratum(controls_by_stratum).items()
                if k in events}
    lifts: List[float] = []
    choices = rng.choices
    for _ in range(spec.bootstrap_iterations):
        drawn_events = {k: choices(v, k=len(v)) for k, v in events.items()}
        drawn_controls = {k: choices(v, k=len(v)) for k, v in controls.items()}
        lift = _lift_from_values(drawn_events, drawn_controls)
        if lift is not None:
            lifts.append(lift)
    if len(lifts) < 2:
        return None, None
    lifts.sort()
    tail = (1.0 - spec.bootstrap_confidence) / 2.0
    lo = lifts[max(0, int(tail * len(lifts)))]
    hi = lifts[min(len(lifts) - 1, int((1.0 - tail) * len(lifts)))]
    return lo, hi


def _permutation_p_value(events_by_stratum, controls_by_stratum,
                         observed: Optional[float], spec: MatchingSpec
                         ) -> Optional[float]:
    """Stratified permutation, assuming no distribution.

    Within each stratum the event and control observations are pooled and
    relabelled at random, preserving both counts. Under the null -- the
    event tells you nothing the stratum did not already -- the labels are
    exchangeable, so the observed lift should look like a typical
    permuted one. The test is two-sided on the magnitude.
    """
    if observed is None:
        return None
    rng = random.Random(spec.permutation_seed)
    events = _values_by_stratum(events_by_stratum)
    controls = _values_by_stratum(controls_by_stratum)
    pooled = {k: v + controls.get(k, []) for k, v in events.items()}
    sizes = {k: len(v) for k, v in events.items()}
    shuffle = rng.shuffle
    extreme = 0
    trials = 0
    target = abs(observed)
    for _ in range(spec.permutation_iterations):
        drawn_events, drawn_controls = {}, {}
        for key, pool in pooled.items():
            shuffled = list(pool)
            shuffle(shuffled)
            n = sizes[key]
            drawn_events[key] = shuffled[:n]
            drawn_controls[key] = shuffled[n:]
        lift = _lift_from_values(drawn_events, drawn_controls)
        if lift is None:
            continue
        trials += 1
        if abs(lift) >= target:
            extreme += 1
    if trials == 0:
        return None
    # Add-one: a p-value of exactly zero would claim more certainty than
    # a finite number of permutations can supply.
    return (extreme + 1) / (trials + 1)


# --- building the two arms from a stage's records ---------------------

class ControlPool:
    """Every eligible non-event moment, stratified and capped.

    RESERVOIR SAMPLED PER STRATUM. A five-year stage offers ~1.7M
    candidate anchors and a stratum needs a few hundred to estimate its
    mean; measuring a forward path at every one would cost far more than
    the answer is worth. Taking the FIRST n of each stratum would bias
    the control toward 2021, so each stratum keeps a uniform sample of
    everything it saw, drawn from a seeded generator: deterministic
    across runs, unbiased across the window.
    """

    def __init__(self, spec: MatchingSpec):
        self.spec = spec
        self._rng = random.Random(spec.control_sample_seed)
        self._seen: Dict[Tuple[int, str], int] = {}
        self._kept: Dict[Tuple[int, str], List[Anchor]] = {}

    def offer(self, anchor: Anchor) -> None:
        key = anchor.stratum.as_key()
        seen = self._seen.get(key, 0) + 1
        self._seen[key] = seen
        kept = self._kept.setdefault(key, [])
        cap = self.spec.max_controls_per_stratum
        if len(kept) < cap:
            kept.append(anchor)
            return
        j = self._rng.randrange(seen)
        if j < cap:
            kept[j] = anchor

    def anchors(self) -> List[Anchor]:
        out = [a for v in self._kept.values() for a in v]
        out.sort(key=lambda a: a.at)      # deterministic order
        return out

    def summary(self) -> dict:
        return {"strata": len(self._kept),
                "candidates_seen": sum(self._seen.values()),
                "anchors_kept": sum(len(v) for v in self._kept.values())}


def _observation_from_path(path: ForwardPath, stratum_key, session_date: str,
                           direction: Direction, scanner,
                           atr: Optional[Decimal],
                           spec: MatchingSpec) -> Optional[Observation]:
    if path.bars_observed == 0:
        return None
    hit_first: Optional[bool] = None
    if atr is not None and atr > 0 and scanner is not None:
        threshold = Decimal(spec.hit_first_atr_multiple) * Decimal(atr)
        if threshold > 0:
            outcome, _when = scanner.hit_first(direction, threshold, threshold)
            if outcome == "favorable":
                hit_first = True
            elif outcome == "adverse":
                hit_first = False
            # "neither" and "same_bar" are genuinely unknown, not False
    return Observation(
        stratum_key=stratum_key,
        session_date=session_date,
        signed_return=float(path.signed_return(direction)),
        mfe=float(path.mfe(direction)),
        mae=float(path.mae(direction)),
        favorable_first=path.favorable_came_first(direction),
        hit_first_favorable=hit_first,
    )


def measure_anchor_paths(anchors: Sequence[Anchor], bars: Sequence[Bar],
                         horizon_minutes: int,
                         bar_index: Optional[BarWindowIndex] = None) -> List:
    """Forward paths for a set of anchors, once, before any direction.

    SPLIT OUT ON PURPOSE. A forward path depends on the anchor, the bars
    and the horizon -- not on which hypothesis is asking or which way it
    faces. The frozen set has twelve hypotheses across three distinct
    horizons, so measuring the control pool per hypothesis did the same
    work four times over. On the real sample that was the difference
    between minutes and hours.

    Returns [(anchor, ForwardPath, PathScanner)].
    """
    if bar_index is None:
        bar_index = BarWindowIndex(bars)
    out = []
    for anchor in anchors:
        measured = measure_forward_path("control", anchor.at, bars,
                                        horizon_minutes, bar_index)
        if measured is not None:
            out.append((anchor, measured[0], measured[1]))
    return out


def observations_from_paths(measured: Sequence, direction: Direction,
                            spec: MatchingSpec,
                            exclude: Optional[Set[datetime]] = None
                            ) -> List[Observation]:
    """Orient measured paths for one hypothesis.

    `exclude` drops anchors that ARE this hypothesis's events, so the
    control arm never contains the very moments it is the comparison
    for. Applied here rather than before measurement, because the
    measurement is shared across hypotheses and the exclusion is not.
    """
    out: List[Observation] = []
    for anchor, path, scanner in measured:
        if exclude and anchor.at in exclude:
            continue
        observation = _observation_from_path(
            path, anchor.stratum.as_key(), anchor.session_date.isoformat(),
            direction, scanner, anchor.atr, spec)
        if observation is not None:
            out.append(observation)
    return out


def measure_anchors(anchors: Sequence[Anchor], bars: Sequence[Bar],
                    direction: Direction, horizon_minutes: int,
                    spec: MatchingSpec,
                    bar_index: Optional[BarWindowIndex] = None
                    ) -> List[Observation]:
    """Measure and orient in one step. Uses exactly the same
    `measure_forward_path` the event arm uses, so the two arms cannot
    differ in how a path is measured -- only in where it is anchored."""
    return observations_from_paths(
        measure_anchor_paths(anchors, bars, horizon_minutes, bar_index),
        direction, spec)


class StageArms:
    """Builds the event and control arms for one stage.

    One pass over the stage's bars assigns every eligible moment a
    stratum; events are looked up in the same map, so an event and a
    control in the same cell are matched by construction rather than by
    a separate and possibly inconsistent rule.
    """

    def __init__(self, calendar, spec: MatchingSpec = MatchingSpec()):
        self.calendar = calendar
        self.spec = spec
        self.regime = VolatilityRegime(spec)
        self._anchor_by_time: Dict[datetime, Anchor] = {}
        self._pool = ControlPool(spec)
        self._eligible_sessions: Set[date] = set()

    def observe_session(self, session_date: date, bars: Sequence[Bar],
                        records: Sequence[FeatureRecord],
                        atr_at: Mapping[datetime, Decimal]) -> None:
        """Stratify one session's bar closes, then bank its ATR.

        The reference is taken BEFORE this session's ATR is banked, so a
        session never contributes to the regime it is judged against.
        """
        reference = self.regime.reference_for(session_date)
        rth_open = self.calendar.rth_open_at(session_date)
        self._eligible_sessions.add(session_date)
        for bar in bars:
            at = bar.closed_at
            atr = atr_at.get(at)
            anchor = Anchor(
                at=at, session_date=session_date,
                stratum=Stratum(
                    time_bucket=time_bucket(at, rth_open,
                                            self.spec.session_minute_bucket),
                    volatility_band=self.regime.band(atr, reference)),
                atr=atr)
            self._anchor_by_time[at] = anchor
        self.regime.observe_session(session_date, records)

    def anchor_at(self, at: datetime) -> Optional[Anchor]:
        return self._anchor_by_time.get(at)

    def build_control_pool(self, exclude: Set[datetime]) -> ControlPool:
        """Every stratified moment that is not one of `exclude`.

        `exclude` is the hypothesis's own event times. Moments merely
        NEAR an event stay in the pool: removing them would define the
        control as "the market when nothing was happening", which is a
        different and much easier comparison. Leaving them in lets some
        event effect bleed into the control, which biases the lift
        TOWARD zero -- the conservative direction.
        """
        pool = ControlPool(self.spec)
        for at in sorted(self._anchor_by_time):
            if at in exclude:
                continue
            pool.offer(self._anchor_by_time[at])
        self._pool = pool
        return pool

    def event_observations(self, events: Sequence[FeatureRecord],
                           bars: Sequence[Bar], direction: Direction,
                           horizon_minutes: int,
                           bar_index: Optional[BarWindowIndex] = None
                           ) -> Tuple[List[Observation], int]:
        """Observations for the event arm, and how many were unstratifiable.

        An event whose moment has no anchor -- because its session was
        gated out, or it fell outside the bars this stage holds -- cannot
        be matched to anything and is counted rather than dropped
        silently.
        """
        anchors, unmatched = [], 0
        for event in events:
            anchor = self._anchor_by_time.get(event.available_at)
            if anchor is None:
                unmatched += 1
                continue
            anchors.append(anchor)
        return (measure_anchors(anchors, bars, direction, horizon_minutes,
                                self.spec, bar_index), unmatched)

    def summary(self) -> dict:
        return {"sessions": len(self._eligible_sessions),
                "anchors": len(self._anchor_by_time),
                "control_pool": self._pool.summary(),
                "matching_spec_digest": self.spec.digest(),
                "baseline_version": BASELINE_VERSION}


# --- the other half of a conditioned hypothesis's falsifier ------------

def compare_conditioning(hypothesis: Hypothesis, partition: str,
                         matched: Sequence[Observation],
                         unmatched: Sequence[Observation],
                         spec: MatchingSpec = MatchingSpec()
                         ) -> BaselineComparison:
    """Does the CONDITION add anything, over the same event unconditioned?

    H2 is H1 plus "above VWAP", and its falsifier has always read: "CI
    includes zero, OR DOES NOT DIFFER FROM THE UNCONDITIONAL CASE". The
    second clause was never implemented -- nothing computed the
    unconditional case -- so half of H2's stated falsifier could not
    fire, and H2 could be reported as supported while adding nothing
    whatever to H1.

    The comparison is against the events of the same type that FAILED
    the condition, not against all of them. Comparing a subset with its
    own superset shares observations between the arms and understates
    the difference; the complement is disjoint, so the contrast is the
    real question -- were the conditioned occurrences different from the
    unconditioned ones?

    The same stratification applies, so a condition that merely selects a
    different time of day or volatility regime does not get credit for it.
    """
    contrast_spec = replace(
        spec, min_controls_per_stratum=spec.min_conditioning_per_stratum)
    result = compare_to_baseline(hypothesis, partition, matched, unmatched,
                                 contrast_spec)
    result.note = (
        "conditioning contrast: events meeting the condition against events "
        "of the same type that did not. Implements the second clause of the "
        "falsifier -- a lift interval spanning zero means the condition adds "
        "nothing over the unconditional case." + (
            f" {result.note}" if result.note else "")
    )
    return result
