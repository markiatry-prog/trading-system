"""Baseline-relative lift: does the event change anything the clock did not?

THE FAILURE THESE TESTS DESCRIBE. Testing a post-event return against
ZERO cannot tell an edge from the market's own drift. Worse, drift is
not uniform through the day -- the hour after the open behaves nothing
like the middle of the night -- so an event that merely CLUSTERS at an
active time inherits that time's drift and reads as an edge.

The decisive test is `test_a_useless_event_in_a_trending_market_has_no_lift`:
a market with a strong opening drift, and an event that fires only in
that window and predicts nothing whatsoever. Against zero it looks
excellent. Against matched controls it is nothing, which is the truth.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_system.features.engine import FeatureEngine
from trading_system.features.records import (FeatureRecord, FeatureType,
                                             RecordKind)
from trading_system.market_data import Bar
from trading_system.research.baseline import (
    BASELINE_VERSION, Anchor, ControlPool, MatchingSpec, Observation,
    StageArms, Stratum, VolatilityRegime, compare_to_baseline,
    measure_anchors, time_bucket)
from trading_system.research.hypotheses import Direction, HypothesisRegistry
from trading_system.research.outcomes import BarWindowIndex

from tests.fixtures_market import NQ

CAL = FeatureEngine(NQ).calendar
SPEC = MatchingSpec(min_controls_per_stratum=10, max_controls_per_stratum=200,
                    bootstrap_iterations=300, permutation_iterations=300)
HORIZON = 30

# Events spread across the whole session and far apart. A control anchor
# within one horizon BEFORE an event captures that event's move too, so
# events packed closer than the horizon push their own effect into the
# control arm and the measured lift collapses toward zero. That bias is
# real and it is the safe direction, but a fixture saturating it cannot
# demonstrate that a genuine effect is recovered -- so the recovery tests
# use a geometry where most controls are clear of any event.
SPARSE = {"events_only_in_opening": False, "event_stride": 149}


def hypothesis(direction=Direction.UP, horizon=HORIZON, hid="HX"):
    r = HypothesisRegistry()
    return r.register(hid, "a synthetic claim", "synthetic_event", direction,
                      horizon, {}, "the confidence interval includes zero")


# --- a market whose drift depends on the time of day ------------------

def synthetic_market(n_sessions=40, minutes=240, seed=7,
                     opening_drift=3.0, later_drift=0.05,
                     opening_window=90, event_bump=0.0, bump_bars=3,
                     event_stride=7, events_only_in_opening=True):
    """Bars, and the moments a synthetic event fired.

    `opening_drift` applies for the first `opening_window` minutes of
    each session and `later_drift` after it, so time of day genuinely
    matters. `event_bump` adds a per-bar move for `bump_bars` after each
    event -- zero for a useless event, positive for a real one.

    TWO PROPERTIES OF THE GEOMETRY, BOTH DELIBERATE.

    Events fire only in the first 25 minutes, and the drift regime does
    not change until minute 90, so an event's whole 30-minute forward
    window and the whole window of every control it is matched against
    sit inside one drift regime. A window straddling the change would
    make matched anchors genuinely unlike each other and the estimator
    would be right to report a difference -- but the test would then be
    measuring the fixture, not the method.

    The bump is SHORT. It has to be: a bump lasting a full horizon,
    fired repeatedly, lands inside the forward windows of the control
    anchors too, and the control then absorbs the very effect the lift
    is trying to measure. That contamination is real and biases lift
    toward zero, which is the safe direction -- but a fixture that
    saturates it cannot demonstrate recovery.
    """
    import random
    rnd = random.Random(seed)
    bars, event_times = [], []
    for s in range(n_sessions):
        session_date = date(2026, 1, 5) + timedelta(days=s)
        if session_date.weekday() >= 5:
            continue
        open_at = CAL.rth_open_at(session_date)
        # A holiday still has a nominal RTH open, but session_date_for
        # maps its bars into the NEXT session -- which would put them 48
        # half-hour buckets before that session's open and silently
        # wreck the matching. Keep only self-consistent days.
        if CAL.session_date_for(open_at) != session_date:
            continue
        px = 20000.0
        bump_left = 0
        fire_at = set()
        if events_only_in_opening:
            candidates = range(5, 26)
        else:
            candidates = range(5, minutes - HORIZON - 5)
        fire_at = {i for i in candidates if (i + s * 3) % event_stride == 0}
        for i in range(minutes):
            drift = opening_drift if i < opening_window else later_drift
            if bump_left > 0:
                drift += event_bump
                bump_left -= 1
            close = px + drift + rnd.uniform(-2.0, 2.0)
            high = max(px, close) + rnd.uniform(0, 1.5)
            low = min(px, close) - rnd.uniform(0, 1.5)
            at = open_at + timedelta(minutes=i)
            bars.append(Bar(
                instrument=NQ, interval_seconds=60,
                open=Decimal(str(round(px, 2))), high=Decimal(str(round(high, 2))),
                low=Decimal(str(round(low, 2))), close=Decimal(str(round(close, 2))),
                volume=100, observed_at=at,
                captured_at=at + timedelta(milliseconds=50), provider="synthetic"))
            if i in fire_at:
                event_times.append(at + timedelta(minutes=1))   # bar CLOSE
                bump_left = bump_bars
            px = close
    return bars, event_times


def build_arms(bars, event_times, spec=SPEC):
    """Stratify the market, then split it into the two arms."""
    arms = StageArms(CAL, spec)
    by_day = {}
    for b in bars:
        by_day.setdefault(CAL.session_date_for(b.observed_at), []).append(b)
    for day in sorted(by_day):
        arms.observe_session(day, by_day[day], [], {})
    events = [
        FeatureRecord(instrument_symbol="NQZ6", kind=RecordKind.EVENT,
                      type="synthetic_event", effective_at=t, available_at=t,
                      session_date=CAL.session_date_for(t).isoformat())
        for t in event_times
    ]
    return arms, events


def run_comparison(bars, event_times, direction=Direction.UP, spec=SPEC,
                   horizon=HORIZON):
    arms, events = build_arms(bars, event_times, spec)
    index = BarWindowIndex(bars)
    event_obs, unmatched = arms.event_observations(
        events, bars, direction, horizon, index)
    pool = arms.build_control_pool({e.available_at for e in events})
    control_obs = measure_anchors(pool.anchors(), bars, direction, horizon,
                                  spec, index)
    h = hypothesis(direction, horizon)
    return compare_to_baseline(h, "discovery", event_obs, control_obs, spec), \
        unmatched


# --- the headline -----------------------------------------------------

def test_a_useless_event_in_a_trending_market_has_no_lift():
    """The whole reason for this module.

    The event fires only during the strongly drifting opening window and
    predicts nothing. Measured against ZERO it is a large, confident
    positive. Measured against controls from the same time of day it is
    approximately nothing.
    """
    bars, events = synthetic_market(event_bump=0.0)
    result, _ = run_comparison(bars, events)

    assert result.event.n > 60, f"only {result.event.n} events; too weak"
    absolute = result.event.mean_signed_return
    assert absolute > 20, (
        f"the synthetic drift did not produce a spurious absolute effect "
        f"({absolute}); the test cannot demonstrate anything")

    assert result.lift is not None
    assert abs(result.lift) < absolute / 4, (
        f"lift {result.lift:.2f} is not small against the spurious absolute "
        f"effect {absolute:.2f}")
    assert result.lift_ci_low < 0 < result.lift_ci_high, (
        f"a useless event produced a lift interval excluding zero: "
        f"[{result.lift_ci_low:.3f}, {result.lift_ci_high:.3f}]")
    assert result.lift_p_value > 0.05


def test_the_standardised_control_is_the_comparator_not_the_raw_pool():
    """Names the mechanism, and the trap inside it.

    The raw control arm spans the whole session, most of which barely
    drifts, so its mean is far below the event arm's -- and reading THAT
    as the baseline would reinvent the confound, comparing an event that
    fires at the open against the average moment of the day. The
    comparator is the same pool reweighted onto the strata the events
    actually occupy, and it lands near the event mean, which is why the
    lift is ~0.
    """
    bars, events = synthetic_market(event_bump=0.0)
    result, _ = run_comparison(bars, events)

    assert result.control.mean_signed_return < \
        result.event.mean_signed_return / 2, (
            "the raw control pool should be much weaker than the event arm; "
            "if it is not, the fixture has no time-of-day confound to remove")

    standardised = result.control_standardised_mean
    assert standardised is not None
    assert abs(result.event.mean_signed_return - standardised) < \
        result.event.mean_signed_return / 4, (
            f"standardised control {standardised:.2f} is not close to the "
            f"event mean {result.event.mean_signed_return:.2f}")
    assert result.lift == pytest.approx(
        result.event.mean_signed_return - standardised, abs=1e-9)


def test_a_genuine_conditional_effect_is_recovered():
    """The other half: the method must not flatten a real effect.

    Events are sparse and the bump short, so most control anchors sit
    outside any bump window -- see synthetic_market's docstring for why
    a saturating fixture would prove nothing.
    """
    bump, bars_bumped = 15.0, 3
    bars, events = synthetic_market(event_bump=bump, bump_bars=bars_bumped,
                                    **SPARSE)
    result, _ = run_comparison(bars, events)
    assert result.lift is not None
    expected = bump * bars_bumped
    assert result.lift > expected * 0.5, (
        f"recovered lift {result.lift:.2f} against an injected "
        f"{expected:.2f}")
    assert result.lift_ci_low > 0, (
        f"a real effect produced an interval including zero: "
        f"[{result.lift_ci_low:.3f}, {result.lift_ci_high:.3f}]")
    assert result.lift_p_value < 0.05


def test_a_real_effect_is_distinguished_from_a_useless_one():
    """Both markets trend. Only one has an event that means anything.

    Same geometry on both sides, so the only difference is whether the
    event predicts anything.
    """
    useless, _ = run_comparison(*synthetic_market(event_bump=0.0, **SPARSE))
    genuine, _ = run_comparison(*synthetic_market(
        event_bump=15.0, bump_bars=3, **SPARSE))
    assert genuine.lift > useless.lift
    assert genuine.lift_excludes_zero is True
    assert useless.lift_excludes_zero is False


def test_a_downward_effect_is_recovered_under_a_down_hypothesis():
    """Direction is declared, never inferred: a falling market must give
    a POSITIVE lift to a hypothesis that predicted the fall."""
    bars, events = synthetic_market(event_bump=-15.0, bump_bars=3, **SPARSE)
    result, _ = run_comparison(bars, events, direction=Direction.DOWN)
    assert result.lift > 0
    assert result.lift_ci_low > 0


def test_an_event_predicting_the_wrong_way_produces_a_negative_lift():
    """Which is what feeds OPPOSITE_EFFECT rather than a confirmation."""
    bars, events = synthetic_market(event_bump=-15.0, bump_bars=3, **SPARSE)
    result, _ = run_comparison(bars, events, direction=Direction.UP)
    assert result.lift < 0
    assert result.lift_ci_high < 0


# --- excursion and ordering lift --------------------------------------

def test_the_excursion_and_ordering_lifts_are_reported():
    bars, events = synthetic_market(event_bump=15.0, bump_bars=3, **SPARSE)
    result, _ = run_comparison(bars, events)
    assert result.mfe_lift is not None and result.mfe_lift > 0
    assert result.mae_lift is not None
    assert result.favorable_first_lift is not None
    row = result.as_row()
    for key in ("event", "control", "absolute_effect", "lift", "lift_ci_low",
                "lift_ci_high", "lift_p_value", "mfe_lift", "mae_lift",
                "favorable_first_lift", "hit_first_lift", "strata_used",
                "baseline_version", "matching_spec_digest"):
        assert key in row, key
    assert row["event"]["n"] > 0 and row["control"]["n"] > 0


def test_the_hit_first_lift_needs_an_atr_and_says_so_when_absent():
    """+X before -Y is expressed in ATR multiples, so it is unavailable
    when no ATR was knowable. Unavailable must not read as False."""
    bars, events = synthetic_market(event_bump=15.0, bump_bars=3, **SPARSE)
    result, _ = run_comparison(bars, events)
    assert result.event.hit_first_favorable_fraction is None, (
        "these fixtures supply no ATR, so the threshold question cannot "
        "be answered and must stay None")

    anchor = Anchor(at=bars[10].closed_at, session_date=date(2026, 1, 5),
                    stratum=Stratum(0, "unknown"), atr=Decimal("5"))
    obs = measure_anchors([anchor], bars, Direction.UP, HORIZON, SPEC)
    assert obs and obs[0].hit_first_favorable is not None


# --- matching mechanics -----------------------------------------------

def test_time_of_day_is_the_matching_axis_that_does_the_work():
    bars, events = synthetic_market(event_bump=0.0)
    arms, event_records = build_arms(bars, events)
    buckets = {arms.anchor_at(e.available_at).stratum.time_bucket
               for e in event_records if arms.anchor_at(e.available_at)}
    assert buckets == {0}, (
        f"events should sit in the first opening bucket only, got {buckets}")
    pool = arms.build_control_pool({e.available_at for e in event_records})
    control_buckets = {a.stratum.time_bucket for a in pool.anchors()}
    assert control_buckets > buckets, "controls must span more of the day"


def test_the_event_times_themselves_are_never_in_the_control_pool():
    bars, events = synthetic_market()
    arms, event_records = build_arms(bars, events)
    excluded = {e.available_at for e in event_records}
    pool = arms.build_control_pool(excluded)
    assert not (excluded & {a.at for a in pool.anchors()})


def test_an_event_with_no_stratifiable_moment_is_counted_not_dropped():
    bars, events = synthetic_market()
    arms, event_records = build_arms(bars, events)
    stray = FeatureRecord(
        instrument_symbol="NQZ6", kind=RecordKind.EVENT, type="synthetic_event",
        effective_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        available_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        session_date="2030-01-01")
    _obs, unmatched = arms.event_observations(
        list(event_records) + [stray], bars, Direction.UP, HORIZON)
    assert unmatched == 1


def test_a_stratum_without_enough_controls_is_dropped_and_reported():
    bars, events = synthetic_market()
    strict = MatchingSpec(min_controls_per_stratum=10_000,
                          bootstrap_iterations=50, permutation_iterations=50)
    result, _ = run_comparison(bars, events, spec=strict)
    assert result.lift is None
    assert result.strata_used == 0
    assert result.events_dropped_for_thin_strata > 0
    assert "cannot be computed" in result.note


def test_sample_sizes_for_both_arms_are_always_reported():
    bars, events = synthetic_market()
    result, _ = run_comparison(bars, events)
    assert result.event.n > 0
    assert result.control.n > 0
    assert result.event.n != result.control.n


# --- volatility regime ------------------------------------------------

def test_the_volatility_reference_uses_only_completed_prior_sessions():
    """A session must never contribute to the regime it is judged
    against; that is what makes the banding free of lookahead."""
    spec = MatchingSpec(volatility_lookback_sessions=3)
    regime = VolatilityRegime(spec)
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(5)]
    values = [10, 20, 30, 40, 50]
    expected = [None, None, None, Decimal(20), Decimal(30)]
    for i, day in enumerate(days):
        assert regime.reference_for(day) == expected[i], day
        regime.observe_session(day, [FeatureRecord(
            instrument_symbol="NQZ6", kind=RecordKind.FEATURE,
            type=FeatureType.ATR.value,
            effective_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
            available_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
            session_date=day.isoformat(), value=Decimal(values[i]))])


def test_bands_separate_quiet_from_violent():
    spec = MatchingSpec()
    regime = VolatilityRegime(spec)
    reference = Decimal("10")
    assert regime.band(Decimal("5"), reference) == "band0"      # quiet
    assert regime.band(Decimal("10"), reference) == "band1"     # normal
    assert regime.band(Decimal("30"), reference) == "band2"     # violent
    assert regime.band(None, reference) == "unknown"
    assert regime.band(Decimal("10"), None) == "unknown"


def test_time_bucket_is_signed_around_the_open():
    open_at = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    assert time_bucket(open_at, open_at, 30) == 0
    assert time_bucket(open_at + timedelta(minutes=29), open_at, 30) == 0
    assert time_bucket(open_at + timedelta(minutes=30), open_at, 30) == 1
    assert time_bucket(open_at - timedelta(minutes=1), open_at, 30) == -1


# --- determinism and no lookahead -------------------------------------

def test_the_whole_comparison_is_deterministic():
    bars, events = synthetic_market()
    a, _ = run_comparison(bars, events)
    b, _ = run_comparison(bars, events)
    assert a.as_row() == b.as_row()


def test_the_control_pool_is_a_uniform_sample_not_the_earliest_anchors():
    """Taking the first n of each stratum would make the control arm a
    2021 sample compared against five years of events."""
    bars, events = synthetic_market()
    arms, event_records = build_arms(bars, events)
    pool = arms.build_control_pool({e.available_at for e in event_records})
    days = {a.session_date for a in pool.anchors()}
    all_days = {CAL.session_date_for(b.observed_at) for b in bars}
    assert len(days) >= len(all_days) - 1, (
        f"control anchors cover {len(days)} of {len(all_days)} sessions")


def test_reservoir_sampling_is_uniform_enough_to_be_unbiased():
    spec = MatchingSpec(max_controls_per_stratum=50)
    pool = ControlPool(spec)
    base = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    for i in range(1000):
        pool.offer(Anchor(at=base + timedelta(minutes=i),
                          session_date=date(2026, 1, 5),
                          stratum=Stratum(0, "unknown"), atr=None))
    kept = pool.anchors()
    assert len(kept) == 50
    late = sum(1 for a in kept if (a.at - base).total_seconds() / 60 >= 500)
    assert 15 <= late <= 35, f"{late}/50 from the second half; not uniform"


def test_no_control_measurement_reaches_beyond_its_own_horizon():
    """The control arm uses the same measure_forward_path as the event
    arm, so it inherits the same bound; asserted rather than assumed."""
    bars, _ = synthetic_market(n_sessions=3)
    anchor_at = bars[10].closed_at
    anchor = Anchor(at=anchor_at, session_date=date(2026, 1, 5),
                    stratum=Stratum(0, "unknown"), atr=None)
    truncated = [b for b in bars if b.closed_at <= anchor_at + timedelta(minutes=HORIZON)]
    full = measure_anchors([anchor], bars, Direction.UP, HORIZON, SPEC)
    prefix = measure_anchors([anchor], truncated, Direction.UP, HORIZON, SPEC)
    assert full and prefix
    assert full[0].signed_return == prefix[0].signed_return, (
        "the measurement changed when later bars were removed, so it was "
        "reading past its horizon")


def test_the_matching_spec_is_hashed_into_every_result():
    bars, events = synthetic_market()
    a, _ = run_comparison(bars, events)
    other = MatchingSpec(session_minute_bucket=15, min_controls_per_stratum=10,
                         max_controls_per_stratum=200,
                         bootstrap_iterations=50, permutation_iterations=50)
    b, _ = run_comparison(bars, events, spec=other)
    assert a.spec_digest != b.spec_digest
    assert a.as_row()["matching_spec_digest"] == a.spec_digest


# --- the second clause of H2's falsifier ------------------------------

def test_a_condition_that_adds_nothing_shows_no_conditioning_lift():
    """H2's falsifier says "does not differ from the unconditional case".
    Nothing computed that case, so half of it could never fire."""
    from trading_system.research.baseline import compare_conditioning
    # The clustered geometry on purpose: every event lands in one time
    # bucket, so this test asks only whether the CONDITION matters, with
    # the time-of-day question already held constant.
    bars, events = synthetic_market(event_bump=0.0)
    arms, records = build_arms(bars, events)
    index = BarWindowIndex(bars)
    obs, _ = arms.event_observations(records, bars, Direction.UP, HORIZON, index)
    # An arbitrary split standing in for a condition that selects
    # nothing meaningful: the two halves must not differ.
    a, b = obs[::2], obs[1::2]
    h = hypothesis()
    result = compare_conditioning(h, "discovery", a, b, SPEC)
    assert result.lift is not None
    assert result.lift_ci_low < 0 < result.lift_ci_high, (
        f"an inert condition produced a lift interval excluding zero: "
        f"[{result.lift_ci_low:.3f}, {result.lift_ci_high:.3f}]")
    assert "unconditional case" in result.note


def test_a_condition_that_selects_the_real_effect_shows_a_conditioning_lift():
    from trading_system.research.baseline import compare_conditioning
    bars, events = synthetic_market(event_bump=40.0, bump_bars=3)
    arms, records = build_arms(bars, events)
    index = BarWindowIndex(bars)
    obs, _ = arms.event_observations(records, bars, Direction.UP, HORIZON, index)
    # Stand in for a condition that isolates the strongest occurrences.
    ranked = sorted(obs, key=lambda o: o.signed_return)
    weak, strong = ranked[:len(ranked) // 2], ranked[len(ranked) // 2:]
    result = compare_conditioning(hypothesis(), "discovery", strong, weak, SPEC)
    assert result.lift > 0
    assert result.lift_ci_low > 0


# --- the inference unit: sessions, not events -------------------------
# Events inside one session share a regime, a news cycle and often
# overlapping forward windows. Resampling them as independent draws
# turns one unusual week into hundreds of confirmations.

def clustered_market(n_sessions=30, hot_sessions=3, events_per_hot=40,
                     events_per_cold=1, bump=25.0, bump_bars=3,
                     minutes=240, seed=11):
    """A market where almost all events come from a handful of sessions.

    The "hot" sessions carry a real post-event move; the rest carry
    events that predict nothing. The effect is therefore supported by
    `hot_sessions` clusters, however many events they contain -- which
    is precisely what event-level inference cannot see.
    """
    import random
    rnd = random.Random(seed)
    bars, event_times = [], []
    index = 0
    for s in range(n_sessions * 2):
        session_date = date(2026, 1, 5) + timedelta(days=s)
        if session_date.weekday() >= 5:
            continue
        open_at = CAL.rth_open_at(session_date)
        if CAL.session_date_for(open_at) != session_date:
            continue
        if index >= n_sessions:
            break
        hot = index < hot_sessions
        n_events = events_per_hot if hot else events_per_cold
        stride = max(1, (minutes - HORIZON - 10) // max(1, n_events))
        fire_at = {5 + i * stride for i in range(n_events)}
        px, bump_left = 20000.0, 0
        for i in range(minutes):
            drift = rnd.uniform(-1.0, 1.0)
            if bump_left > 0 and hot:
                drift += bump
                bump_left -= 1
            close = px + drift
            at = open_at + timedelta(minutes=i)
            bars.append(Bar(
                instrument=NQ, interval_seconds=60,
                open=Decimal(str(round(px, 2))),
                high=Decimal(str(round(max(px, close) + 0.5, 2))),
                low=Decimal(str(round(min(px, close) - 0.5, 2))),
                close=Decimal(str(round(close, 2))), volume=100,
                observed_at=at, captured_at=at + timedelta(milliseconds=50),
                provider="synthetic"))
            if i in fire_at and i < minutes - HORIZON - 5:
                event_times.append(at + timedelta(minutes=1))
                bump_left = bump_bars
            px = close
        index += 1
    return bars, event_times


def test_many_events_from_few_sessions_do_not_give_a_narrow_interval():
    """The headline requirement.

    Almost every event comes from three sessions. Event-level inference
    sees hundreds of confirmations; session-clustered inference sees
    three, and its interval must be far wider for it.
    """
    bars, events = clustered_market()
    result, _ = run_comparison(bars, events)

    assert result.event.n > 100, f"only {result.event.n} events"
    assert result.unique_sessions > 10, "the control arm spans many sessions"
    assert result.effective_clusters is not None
    assert result.effective_clusters < 10, (
        f"effective clusters {result.effective_clusters:.1f} should collapse "
        f"toward the handful of sessions actually carrying the effect")

    clustered_width = result.lift_ci_high - result.lift_ci_low
    naive_width = result.event_level_ci_high - result.event_level_ci_low
    assert clustered_width > naive_width * 2, (
        f"clustered interval {clustered_width:.2f} is not materially wider "
        f"than the event-level {naive_width:.2f}; the clustering is not "
        f"doing anything")


def test_the_effective_cluster_count_exposes_the_concentration():
    bars, events = clustered_market()
    result, _ = run_comparison(bars, events)
    assert result.effective_clusters < result.event.n / 10, (
        f"{result.event.n} events but only "
        f"{result.effective_clusters:.1f} effective clusters -- the ratio is "
        f"the warning, and it must be visible")
    row = result.as_row()
    for key in ("inference_unit", "unique_sessions", "effective_clusters",
                "event_level_ci_low", "event_level_ci_high",
                "event_level_p_value", "clustering_changes_the_conclusion"):
        assert key in row, key
    assert row["inference_unit"] == "session_cluster"


def test_evenly_spread_events_are_not_penalised_by_clustering():
    """Clustering must be calibrated, not merely conservative: when
    events really are spread across many sessions, the two intervals
    should be comparable."""
    bars, events = clustered_market(n_sessions=30, hot_sessions=30,
                                    events_per_hot=2, events_per_cold=2)
    result, _ = run_comparison(bars, events)
    assert result.effective_clusters > 20, (
        f"effective clusters {result.effective_clusters:.1f} should be near "
        f"the session count when events are spread evenly")
    clustered_width = result.lift_ci_high - result.lift_ci_low
    naive_width = result.event_level_ci_high - result.event_level_ci_low
    assert clustered_width < naive_width * 3, (
        f"clustered {clustered_width:.2f} vs event-level {naive_width:.2f}: "
        f"clustering is inflating an interval it should barely change")


def test_effective_clusters_is_the_session_count_when_spread_evenly():
    obs = [Observation(stratum_key=(0, "unknown"), session_date=f"2026-01-{d:02d}",
                       signed_return=1.0, mfe=1.0, mae=0.0,
                       favorable_first=True, hit_first_favorable=None)
           for d in range(1, 11) for _ in range(5)]
    from trading_system.research.baseline import effective_clusters
    assert effective_clusters(obs) == pytest.approx(10.0)


def test_effective_clusters_collapses_when_one_session_dominates():
    from trading_system.research.baseline import effective_clusters
    obs = ([Observation((0, "u"), "2026-01-01", 1.0, 1.0, 0.0, True, None)] * 100
           + [Observation((0, "u"), f"2026-01-{d:02d}", 1.0, 1.0, 0.0, True, None)
              for d in range(2, 5)])
    # 103 observations, but one session holds 100 of them.
    assert effective_clusters(obs) < 1.2


def test_too_few_sessions_reports_no_interval_rather_than_a_narrow_one():
    strict = MatchingSpec(min_controls_per_stratum=5,
                          max_controls_per_stratum=200,
                          min_sessions_for_inference=500,
                          bootstrap_iterations=100, permutation_iterations=100)
    bars, events = clustered_market()
    result, _ = run_comparison(bars, events, spec=strict)
    assert result.lift is not None, "the point estimate is still computable"
    assert result.lift_ci_low is None and result.lift_p_value is None
    assert "below the" in result.note and "resample clusters" in result.note


def test_a_session_drawn_twice_contributes_twice():
    """The defining property of a cluster bootstrap, asserted directly."""
    from trading_system.research.baseline import cluster_contributions
    obs = [Observation((0, "u"), "2026-01-01", 2.0, 2.0, 0.0, True, None),
           Observation((0, "u"), "2026-01-01", 4.0, 4.0, 0.0, True, None),
           Observation((1, "u"), "2026-01-02", 6.0, 6.0, 0.0, True, None)]
    contributions = cluster_contributions(obs)
    assert contributions["2026-01-01"][(0, "u")] == (6.0, 2)
    assert contributions["2026-01-02"][(1, "u")] == (6.0, 1)
    assert set(contributions) == {"2026-01-01", "2026-01-02"}


def test_every_observation_records_the_session_it_came_from():
    bars, events = synthetic_market()
    arms, records = build_arms(bars, events)
    index = BarWindowIndex(bars)
    obs, _ = arms.event_observations(records, bars, Direction.UP, HORIZON, index)
    assert obs
    assert all(o.session_date for o in obs)
    assert len({o.session_date for o in obs}) > 1


def test_the_clustered_bootstrap_is_deterministic():
    from trading_system.research.baseline import cluster_bootstrap
    bars, events = clustered_market()
    arms, records = build_arms(bars, events)
    index = BarWindowIndex(bars)
    ev, _ = arms.event_observations(records, bars, Direction.UP, HORIZON, index)
    pool = arms.build_control_pool({e.available_at for e in records})
    ct = measure_anchors(pool.anchors(), bars, Direction.UP, HORIZON, SPEC, index)
    assert cluster_bootstrap(ev, ct, SPEC) == cluster_bootstrap(ev, ct, SPEC)
