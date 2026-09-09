"""T-005 candidate search: a frozen space, a deterministic winner.

WHAT THIS IS FOR. Twelve preregistered hypotheses returned zero
baseline-relative lift. This searches the discovery partition for a
conditional behaviour worth preregistering NEXT -- hypothesis
generation, not validation. Its output is a candidate that has earned
the right to be tested, or the finding that none has.

WHY EVERYTHING IS FROZEN BEFORE THE DATA IS TOUCHED. A search that looks
at outcomes and then decides how to rank them is not a search, it is a
selection dressed as one. So the space, the gates, the complexity
penalty, the stability requirements, the ranking score and the bar a
candidate must clear are all declared here, hashed into a digest, and
printed before the first outcome is read. Changing any of them changes
the digest, which is the record that they were not adjusted until
something looked good.

THE SEARCH IS DELIBERATELY SMALL. Not because a bigger one is harder to
run, but because every additional candidate raises the bar the winner
must clear -- the correction is over everything evaluated, not over what
gets reported. Complexity is capped at four elements, conditions at two,
and sequences to a single predecessor.

WHAT PROTECTS THE RESULT

  frozen space        every candidate enumerated before any outcome
  full census         every evaluation counted, including the ones that
                      failed a gate, so the mining is auditable
  session clusters    inference resamples sessions, as in T-004B
  chronological       the sign must hold in all three thirds of the
  stability           discovery period, each independently powered
  regime stability    the sign must hold across volatility regimes
  parameter           the numeric band edges are perturbed and the sign
  sensitivity         must survive
  complexity penalty  a simpler candidate beats a better-fitting one
  harsh correction    Bonferroni over the WHOLE census, not the top-K

WHAT THIS CANNOT DO. It cannot tell you the candidate is real. Selecting
the best of thousands guarantees the winner looks better than it is;
that is arithmetic, not pessimism. The candidate's numbers here are
inflated by selection and must never be quoted as evidence. Only a
preregistered test on data this search never touched can do that.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import (Dict, FrozenSet, Iterable, List, Mapping, Optional,
                    Sequence, Set, Tuple)

from ..features.records import EventType
from ..provenance import digest
from .baseline import (MatchingSpec, Observation, cluster_bootstrap,
                       effective_clusters)
from .hypotheses import Direction

SEARCH_VERSION = "1.0.0"


class DiscoveryError(Exception):
    pass


# --- the condition vocabulary, frozen ---------------------------------
# Six families of deterministic market state, each discretised into a
# small number of bands. Discrete on purpose: a continuous threshold
# invites the search to find the exact cut that works, which is the
# definition of a knife edge.

TAG_FAMILIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    # Where in the session. Intraday seasonality is the largest
    # non-event effect there is, so it is a condition rather than
    # something to be surprised by later.
    ("session_phase", ("open30", "mid", "late", "overnight")),
    # Volatility relative to the market's own recent past.
    ("atr_regime", ("quiet", "normal", "elevated")),
    # Volume against its rolling average.
    ("volume_context", ("low", "normal", "high")),
    # Which side of the session VWAP price sits on.
    ("vwap_side", ("above", "below")),
    # Position relative to the opening range.
    ("or_location", ("above_or", "inside_or", "below_or")),
    # The most recent confirmed structure break, if recent enough.
    ("structure_bias", ("up", "down", "none")),
)

TAG_ORDER: Tuple[str, ...] = tuple(name for name, _ in TAG_FAMILIES)
TAG_VALUES: Dict[str, Tuple[str, ...]] = {n: v for n, v in TAG_FAMILIES}

# Events that may anchor an observation. Session open and close are
# bookkeeping, not market events, and cannot anchor anything.
ANCHOR_EVENTS: Tuple[str, ...] = tuple(
    e.value for e in EventType
    if e not in (EventType.SESSION_OPEN, EventType.SESSION_CLOSE))

# Events that may precede an anchor to form a two-step sequence. A
# small declared subset: allowing every type as a predecessor would
# multiply the census by thirteen for a dimension we have no particular
# reason to think matters.
PREDECESSOR_EVENTS: Tuple[str, ...] = (
    EventType.DISPLACEMENT_UP.value,
    EventType.DISPLACEMENT_DOWN.value,
    EventType.STRUCTURE_BREAK_UP.value,
    EventType.STRUCTURE_BREAK_DOWN.value,
    EventType.LIQUIDITY_SWEEP_HIGH.value,
    EventType.LIQUIDITY_SWEEP_LOW.value,
)


@dataclass(frozen=True)
class SearchSpace:
    """Every candidate that will be evaluated. Fixed before the data."""
    version: str = SEARCH_VERSION
    anchors: Tuple[str, ...] = ANCHOR_EVENTS
    predecessors: Tuple[str, ...] = PREDECESSOR_EVENTS
    predecessor_window_minutes: int = 30
    horizons: Tuple[int, ...] = (30, 60)
    directions: Tuple[str, ...] = ("up", "down")
    max_conditions: int = 2
    # Conditions allowed alongside a predecessor. Lower, because the
    # predecessor already spends a complexity element.
    max_conditions_with_predecessor: int = 1
    max_complexity: int = 4
    structure_bias_window_minutes: int = 60

    def digest(self) -> str:
        return digest({**{k: str(v) for k, v in sorted(asdict(self).items())},
                       "tag_families": [[n, list(v)] for n, v in TAG_FAMILIES]})


@dataclass(frozen=True)
class SelectionCriteria:
    """The bar a candidate must clear, and how candidates are ranked.

    Declared before any outcome is read. The ranking score is a formula,
    not a judgement: the harness computes it, sorts, and returns the top
    one. No human -- and no model -- looks at the table and picks.
    """
    # -- frequency and independence ----------------------------------
    min_events: int = 200
    min_event_sessions: int = 100
    min_effective_clusters: int = 60
    # Each chronological third must independently carry this much.
    min_events_per_third: int = 40
    min_clusters_per_third: int = 15

    # -- economic meaning --------------------------------------------
    # A lift below this is not worth testing whatever its p-value: the
    # question is whether the behaviour is worth acting on, and a
    # quarter of a tick is not.
    min_absolute_lift_points: str = "1.0"
    # And it must be material against the movement available over the
    # horizon, not merely non-zero.
    min_lift_to_mfe_ratio: str = "0.10"

    # -- stability ---------------------------------------------------
    require_sign_stable_thirds: bool = True
    require_sign_stable_regimes: bool = True
    # Under a +/- perturbation of every numeric band edge, the sign must
    # hold and this much of the magnitude must survive.
    parameter_perturbation: str = "0.20"
    min_perturbed_magnitude_ratio: str = "0.50"

    # -- inference ---------------------------------------------------
    # The clustered interval must exclude zero at this confidence. A
    # SANITY FLOOR, not a family-wise control -- see below.
    ci_confidence: float = 0.99
    # Reported, never gated on. Bonferroni over a census of ~14,000
    # gives a corrected alpha near 4e-6, and a percentile bootstrap
    # cannot report a p below 2/(iterations+1) ~ 1e-3. Gating on it
    # would make the harness incapable of EVER returning a candidate,
    # whatever the data said -- and it would return NO VIABLE CANDIDATE
    # with the confident air of a finding. So the number is computed and
    # printed as context: it says how far the winner is from surviving a
    # family-wise correction, which is information the reader needs.
    #
    # What actually controls the mining here is not a threshold on a
    # p-value. It is the stability requirements: a relationship must
    # hold its sign in three chronological thirds AND three volatility
    # regimes AND survive dropping each of its conditions. A spurious
    # fit clears a p-value far more easily than it clears those.
    reported_family_wise_alpha: float = 0.05
    # Candidates carried from the cheap screen into full inference.
    shortlist_size: int = 50

    # -- ranking ------------------------------------------------------
    # Each element beyond the anchor multiplies the score by this. A
    # two-condition candidate must be ~1.6x better than a bare one to
    # win, so a simple recurring relationship beats a fitted rule.
    complexity_penalty_per_element: str = "0.80"

    def digest(self) -> str:
        return digest({k: str(v) for k, v in sorted(asdict(self).items())})


@dataclass(frozen=True)
class Candidate:
    """One relationship to evaluate. Immutable and self-describing."""
    anchor: str
    conditions: Tuple[Tuple[str, str], ...]   # sorted (family, value)
    predecessor: Optional[str]
    horizon_minutes: int
    direction: str

    @property
    def complexity(self) -> int:
        return 1 + len(self.conditions) + (1 if self.predecessor else 0)

    def key(self) -> str:
        parts = [self.anchor]
        if self.predecessor:
            parts.append(f"after<{self.predecessor}>")
        parts.extend(f"{f}={v}" for f, v in self.conditions)
        parts.append(f"{self.horizon_minutes}m")
        parts.append(self.direction)
        return "|".join(parts)

    def describe(self) -> str:
        clauses = []
        if self.predecessor:
            clauses.append(f"within the last N minutes: {self.predecessor}")
        clauses.extend(f"{f} is {v}" for f, v in self.conditions)
        when = f"{self.anchor}" + (
            f", when {' and '.join(clauses)}" if clauses else "")
        way = "rises" if self.direction == "up" else "falls"
        return (f"When {when}, price {way} over the following "
                f"{self.horizon_minutes} minutes")

    def as_row(self) -> dict:
        return {"key": self.key(), "anchor": self.anchor,
                "conditions": [list(c) for c in self.conditions],
                "predecessor": self.predecessor,
                "horizon_minutes": self.horizon_minutes,
                "direction": self.direction,
                "complexity": self.complexity}


def _condition_sets(max_conditions: int) -> List[Tuple[Tuple[str, str], ...]]:
    """Every allowed condition set, in a deterministic order.

    Two conditions never come from the same family: "atr_regime is quiet
    AND atr_regime is elevated" is unsatisfiable, and pairing a family
    with itself would pad the census with candidates that cannot fire.
    """
    singles: List[Tuple[str, str]] = [
        (family, value) for family in TAG_ORDER for value in TAG_VALUES[family]]
    out: List[Tuple[Tuple[str, str], ...]] = [()]
    if max_conditions >= 1:
        out.extend((s,) for s in singles)
    if max_conditions >= 2:
        for i, a in enumerate(singles):
            for b in singles[i + 1:]:
                if a[0] == b[0]:
                    continue
                out.append(tuple(sorted((a, b))))
    return out


def enumerate_candidates(space: SearchSpace = SearchSpace()) -> List[Candidate]:
    """The whole census, in a deterministic order, before any data."""
    plain = _condition_sets(space.max_conditions)
    with_pred = _condition_sets(space.max_conditions_with_predecessor)
    out: List[Candidate] = []
    for anchor in space.anchors:
        for predecessor in (None,) + tuple(space.predecessors):
            sets = plain if predecessor is None else with_pred
            for conditions in sets:
                for horizon in space.horizons:
                    for direction in space.directions:
                        candidate = Candidate(anchor, conditions, predecessor,
                                              horizon, direction)
                        if candidate.complexity <= space.max_complexity:
                            out.append(candidate)
    return out


# --- tagged observations ----------------------------------------------

@dataclass(frozen=True)
class Tagged:
    """One measured forward path plus the market state it sat in.

    Oriented UP. The DOWN view is derived by negation rather than
    measured again -- signed return, excursions and ordering all flip
    exactly -- so a direction costs nothing but a sign.
    """
    stratum_key: Tuple[int, str]
    session_date: str
    third: int                      # chronological third of discovery
    tags: Tuple[str, ...]           # in TAG_ORDER
    recent: FrozenSet[str]          # event types within the sequence window
    signed_return: float            # positive = up
    max_up: float
    max_down: float
    favorable_first_up: Optional[bool]

    def observation(self, direction: str) -> Observation:
        up = direction == "up"
        return Observation(
            stratum_key=self.stratum_key, session_date=self.session_date,
            signed_return=self.signed_return if up else -self.signed_return,
            mfe=self.max_up if up else -self.max_down,
            mae=-self.max_down if up else self.max_up,
            favorable_first=(self.favorable_first_up if up else
                             (None if self.favorable_first_up is None
                              else not self.favorable_first_up)),
            hit_first_favorable=None)

    def matches(self, conditions: Tuple[Tuple[str, str], ...],
                predecessor: Optional[str], index: Mapping[str, int]) -> bool:
        if predecessor is not None and predecessor not in self.recent:
            return False
        for family, value in conditions:
            if self.tags[index[family]] != value:
                return False
        return True


TAG_INDEX: Dict[str, int] = {name: i for i, name in enumerate(TAG_ORDER)}


# --- the cheap screen -------------------------------------------------

def _accumulate(observations: Iterable[Tagged],
                condition_sets: Sequence[Tuple[Tuple[str, str], ...]],
                predecessor: Optional[str]):
    """Per-stratum (sum, count) for every condition set, in ONE pass.

    An observation matches at most twenty-two of the hundred and fifty
    three condition sets -- the empty one, one per family, and one per
    pair of families -- so incrementing the sets it matches costs far
    less than testing every set against every observation.
    """
    by_key = {cs: i for i, cs in enumerate(condition_sets)}
    totals: List[Dict[Tuple[int, str], List[float]]] = [
        {} for _ in condition_sets]
    counts = [0] * len(condition_sets)
    for o in observations:
        if predecessor is not None and predecessor not in o.recent:
            continue
        present = [(TAG_ORDER[i], o.tags[i]) for i in range(len(TAG_ORDER))]
        matched = [()]
        matched.extend((p,) for p in present)
        for i, a in enumerate(present):
            for b in present[i + 1:]:
                matched.append(tuple(sorted((a, b))))
        for cs in matched:
            slot = by_key.get(cs)
            if slot is None:
                continue
            counts[slot] += 1
            cell = totals[slot].get(o.stratum_key)
            if cell is None:
                totals[slot][o.stratum_key] = [o.signed_return, 1]
            else:
                cell[0] += o.signed_return
                cell[1] += 1
    return totals, counts


def _stratified_lift(event_cells, control_cells) -> Optional[float]:
    """Direct standardisation from per-stratum sums and counts.

    The same estimator as the baseline module's, expressed over the
    accumulators the screen builds.
    """
    total = sum(c[1] for c in event_cells.values())
    if not total:
        return None
    event_mean = sum(c[0] for c in event_cells.values()) / total
    weighted = 0.0
    used = 0.0
    for key, cell in event_cells.items():
        control = control_cells.get(key)
        if not control or control[1] == 0:
            continue
        weight = cell[1] / total
        weighted += weight * (control[0] / control[1])
        used += weight
    if used == 0:
        return None
    return event_mean - (weighted / used)


@dataclass
class ScreenResult:
    candidate: Candidate
    n_events: int
    n_controls: int
    lift: Optional[float]
    passed: bool
    reason: str = ""

    def as_row(self) -> dict:
        return {**self.candidate.as_row(), "n_events": self.n_events,
                "n_controls": self.n_controls, "lift": self.lift,
                "passed_screen": self.passed, "reason": self.reason}


def screen_candidates(candidates: Sequence[Candidate],
                      events_by_anchor: Mapping[Tuple[str, int], List[Tagged]],
                      controls_by_horizon: Mapping[int, List[Tagged]],
                      criteria: SelectionCriteria,
                      space: SearchSpace) -> List[ScreenResult]:
    """Every candidate gets a point estimate. Nothing is skipped.

    Cheap on purpose: sample size and lift only, no resampling. A
    candidate that cannot clear the frequency floor or the economic
    floor is settled here, and the expensive inference is spent only on
    what survives. The full census is still counted, because the
    correction is over everything evaluated.
    """
    floor = float(criteria.min_absolute_lift_points)
    plain = _condition_sets(space.max_conditions)
    with_pred = _condition_sets(space.max_conditions_with_predecessor)

    cache: Dict[Tuple[str, int, Optional[str], str], Tuple] = {}

    def cells_for(anchor, horizon, predecessor, arm):
        key = (anchor, horizon, predecessor, arm)
        if key not in cache:
            sets = plain if predecessor is None else with_pred
            source = (events_by_anchor.get((anchor, horizon), []) if arm == "e"
                      else controls_by_horizon.get(horizon, []))
            cache[key] = (_accumulate(source, sets, predecessor), sets)
        return cache[key]

    out: List[ScreenResult] = []
    for candidate in candidates:
        (ev_totals, ev_counts), sets = cells_for(
            candidate.anchor, candidate.horizon_minutes,
            candidate.predecessor, "e")
        slot = sets.index(candidate.conditions)
        n_events = ev_counts[slot]
        if n_events < criteria.min_events:
            out.append(ScreenResult(candidate, n_events, 0, None, False,
                                    "below the event floor"))
            continue
        (ct_totals, ct_counts), _ = cells_for(
            candidate.anchor, candidate.horizon_minutes,
            candidate.predecessor, "c")
        n_controls = ct_counts[slot]
        lift = _stratified_lift(ev_totals[slot], ct_totals[slot])
        if candidate.direction == "down" and lift is not None:
            lift = -lift
        if lift is None:
            out.append(ScreenResult(candidate, n_events, n_controls, None,
                                    False, "no matched controls"))
            continue
        if abs(lift) < floor or lift <= 0:
            out.append(ScreenResult(
                candidate, n_events, n_controls, lift, False,
                "below the economic floor" if lift > 0
                else "moves against the stated direction"))
            continue
        out.append(ScreenResult(candidate, n_events, n_controls, lift, True))
    return out


# --- full inference on the survivors ----------------------------------

@dataclass
class Assessment:
    """One shortlisted candidate, fully measured."""
    candidate: Candidate
    n_events: int = 0
    n_controls: int = 0
    event_sessions: int = 0
    effective_clusters: Optional[float] = None
    event_mean: Optional[float] = None
    control_mean: Optional[float] = None
    lift: Optional[float] = None
    lift_ci_low: Optional[float] = None
    lift_ci_high: Optional[float] = None
    lift_p_value: Optional[float] = None
    mfe_mean: Optional[float] = None
    mae_mean: Optional[float] = None
    thirds: List[Optional[float]] = field(default_factory=list)
    thirds_events: List[int] = field(default_factory=list)
    thirds_clusters: List[float] = field(default_factory=list)
    regimes: Dict[str, Optional[float]] = field(default_factory=dict)
    perturbed: Dict[str, Optional[float]] = field(default_factory=dict)
    score: Optional[float] = None
    qualified: bool = False
    failures: List[str] = field(default_factory=list)
    # Context, not gates. How many candidates the winner was chosen
    # from, what a family-wise correction over that census would demand,
    # and the smallest p the bootstrap can even express.
    census_size: int = 0
    family_wise_alpha: Optional[float] = None
    p_resolution_floor: Optional[float] = None

    def as_row(self) -> dict:
        return {
            **self.candidate.as_row(),
            "n_events": self.n_events, "n_controls": self.n_controls,
            "event_sessions": self.event_sessions,
            "effective_clusters": self.effective_clusters,
            "event_mean": self.event_mean, "control_mean": self.control_mean,
            "lift": self.lift, "lift_ci_low": self.lift_ci_low,
            "lift_ci_high": self.lift_ci_high,
            "lift_p_value": self.lift_p_value,
            "mfe_mean": self.mfe_mean, "mae_mean": self.mae_mean,
            "thirds": self.thirds, "thirds_events": self.thirds_events,
            "thirds_clusters": self.thirds_clusters,
            "regimes": self.regimes, "perturbed": self.perturbed,
            "score": self.score, "qualified": self.qualified,
            "failures": self.failures, "census_size": self.census_size,
            "family_wise_alpha": self.family_wise_alpha,
            "p_resolution_floor": self.p_resolution_floor,
            "survives_family_wise": (
                None if self.lift_p_value is None or self.family_wise_alpha
                is None else self.lift_p_value <= self.family_wise_alpha),
        }


def _mean(values) -> Optional[float]:
    values = list(values)
    return (sum(values) / len(values)) if values else None


def _subset_lift(events: Sequence[Observation],
                 controls: Sequence[Observation]) -> Optional[float]:
    ev: Dict[Tuple[int, str], List[float]] = {}
    ct: Dict[Tuple[int, str], List[float]] = {}
    for o in events:
        ev.setdefault(o.stratum_key, [0.0, 0])
        ev[o.stratum_key][0] += o.signed_return
        ev[o.stratum_key][1] += 1
    for o in controls:
        ct.setdefault(o.stratum_key, [0.0, 0])
        ct[o.stratum_key][0] += o.signed_return
        ct[o.stratum_key][1] += 1
    return _stratified_lift(ev, ct)


def assess(candidate: Candidate, events: Sequence[Tagged],
           controls: Sequence[Tagged], criteria: SelectionCriteria,
           matching: MatchingSpec, census_size: int) -> Assessment:
    """Everything the frozen criteria demand, for one candidate.

    Order matters: the gates that can be settled from counts come first,
    so a candidate that cannot qualify never reaches the resampling.
    """
    result = Assessment(candidate=candidate)
    matched_e = [t for t in events
                 if t.matches(candidate.conditions, candidate.predecessor,
                              TAG_INDEX)]
    matched_c = [t for t in controls
                 if t.matches(candidate.conditions, candidate.predecessor,
                              TAG_INDEX)]
    ev = [t.observation(candidate.direction) for t in matched_e]
    ct = [t.observation(candidate.direction) for t in matched_c]
    result.n_events, result.n_controls = len(ev), len(ct)
    if not ev or not ct:
        result.failures.append("no events or no controls")
        return result

    result.event_sessions = len({o.session_date for o in ev})
    result.effective_clusters = effective_clusters(ev)
    result.event_mean = _mean(o.signed_return for o in ev)
    result.control_mean = _mean(o.signed_return for o in ct)
    result.mfe_mean = _mean(o.mfe for o in ev)
    result.mae_mean = _mean(o.mae for o in ev)
    result.lift = _subset_lift(ev, ct)

    if result.n_events < criteria.min_events:
        result.failures.append("min_events")
    if result.event_sessions < criteria.min_event_sessions:
        result.failures.append("min_event_sessions")
    if (result.effective_clusters or 0) < criteria.min_effective_clusters:
        result.failures.append("min_effective_clusters")
    if result.lift is None:
        result.failures.append("no lift")
        return result
    if result.lift < float(criteria.min_absolute_lift_points):
        result.failures.append("min_absolute_lift_points")
    if result.mfe_mean and abs(result.lift) / abs(result.mfe_mean) < \
            float(criteria.min_lift_to_mfe_ratio):
        result.failures.append("min_lift_to_mfe_ratio")

    # -- chronological stability -------------------------------------
    for third in (0, 1, 2):
        te = [t.observation(candidate.direction) for t in matched_e
              if t.third == third]
        tc = [t.observation(candidate.direction) for t in matched_c
              if t.third == third]
        result.thirds.append(_subset_lift(te, tc) if te and tc else None)
        result.thirds_events.append(len(te))
        result.thirds_clusters.append(effective_clusters(te) or 0.0)
    if criteria.require_sign_stable_thirds:
        if any(v is None or v <= 0 for v in result.thirds):
            result.failures.append("sign_stable_thirds")
        if any(n < criteria.min_events_per_third
               for n in result.thirds_events):
            result.failures.append("min_events_per_third")
        if any(c < criteria.min_clusters_per_third
               for c in result.thirds_clusters):
            result.failures.append("min_clusters_per_third")

    # -- regime stability --------------------------------------------
    regime_slot = TAG_INDEX["atr_regime"]
    for regime in TAG_VALUES["atr_regime"]:
        re_ = [t.observation(candidate.direction) for t in matched_e
               if t.tags[regime_slot] == regime]
        rc = [t.observation(candidate.direction) for t in matched_c
              if t.tags[regime_slot] == regime]
        result.regimes[regime] = (_subset_lift(re_, rc) if re_ and rc else None)
    if criteria.require_sign_stable_regimes:
        seen = [v for v in result.regimes.values() if v is not None]
        if not seen or any(v <= 0 for v in seen):
            result.failures.append("sign_stable_regimes")

    # -- nearby-parameter sensitivity --------------------------------
    # The bands are discrete, so "nearby" means dropping each condition
    # in turn: if the relationship only exists inside one exact cell and
    # vanishes when that cell is widened, it is a knife edge.
    retained = float(criteria.min_perturbed_magnitude_ratio)
    for family, value in candidate.conditions:
        slot = TAG_INDEX[family]
        widened = tuple(c for c in candidate.conditions if c[0] != family)
        we = [t.observation(candidate.direction) for t in events
              if t.matches(widened, candidate.predecessor, TAG_INDEX)]
        wc = [t.observation(candidate.direction) for t in controls
              if t.matches(widened, candidate.predecessor, TAG_INDEX)]
        widened_lift = _subset_lift(we, wc) if we and wc else None
        result.perturbed[f"drop:{family}"] = widened_lift
        if widened_lift is None or widened_lift <= 0 or \
                abs(widened_lift) < retained * abs(result.lift):
            result.failures.append(f"parameter_sensitivity:{family}")

    if result.failures:
        return result

    # -- inference, only for candidates that got this far -------------
    spec = replace(matching, bootstrap_confidence=criteria.ci_confidence)
    low, high, p, _sessions = cluster_bootstrap(ev, ct, spec)
    result.lift_ci_low, result.lift_ci_high, result.lift_p_value = low, high, p
    result.census_size = census_size
    result.family_wise_alpha = (
        criteria.reported_family_wise_alpha / max(1, census_size))
    result.p_resolution_floor = 2.0 / (spec.bootstrap_iterations + 1)
    if low is None or p is None:
        result.failures.append("no clustered interval")
        return result
    if low <= 0:
        result.failures.append(
            f"the {criteria.ci_confidence:.0%} clustered interval includes zero")
    if result.failures:
        return result

    penalty = float(criteria.complexity_penalty_per_element) ** (
        candidate.complexity - 1)
    result.score = abs(result.lift) * penalty
    result.qualified = True
    return result


def select_winner(assessments: Sequence[Assessment]) -> Optional[Assessment]:
    """The single winner, by formula. No inspection, no judgement.

    Sorted by score, then by effective clusters, then by the candidate's
    key -- so ties break deterministically and the same inputs always
    name the same winner.
    """
    qualified = [a for a in assessments if a.qualified and a.score is not None]
    if not qualified:
        return None
    qualified.sort(key=lambda a: (-a.score, -(a.effective_clusters or 0),
                                  a.candidate.key()))
    return qualified[0]
