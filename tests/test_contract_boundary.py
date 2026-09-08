"""The contract-boundary research-validity rule.

The claim under test: an observation that compares a prior-session
reference level against the current session is admissible only when
both come from the SAME deliverable futures contract, and that
admissibility is established from symbology provenance rather than from
a calendar.

The headline case is the last test in this file. Two consecutive
sessions each contain one prior-day-high sweep AND one opening-range
break. The rule must refuse the sweep on the session across the roll
while leaving the opening-range break on that same session untouched --
a per-hypothesis exclusion, not a per-session one.
"""
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.features.contracts import (
    EXCLUSION_REASON, ContinuityVerdict, ContractTimeline,
    PRIOR_SESSION_EVENTS, PRIOR_SESSION_FEATURES, SessionContract,
)
from trading_system.features.engine import FeatureEngine
from trading_system.features.records import EventType, FeatureType
from trading_system.market_data import Instrument
from trading_system.research.eligibility import (
    depends_on_prior_session_levels, filter_eligible,
)
from trading_system.research.hypotheses import Direction, HypothesisRegistry
from trading_system.research.partitions import Partition, split_chronologically
from trading_system.research.preregistered import build_registry
from trading_system.research.quality import QualityReport
from trading_system.research.study import Study
from trading_system.sources.databento_source import (
    DatabentoFileSource, DictResolver, contract_id_for, normalize_ohlcv,
)

from tests.fixtures_market import NQ, RTH_OPEN, bar

ROOT = Path(__file__).resolve().parents[1]

# Two contracts of the same product. The numbers are CME security ids as
# Databento reports them; nothing in the code parses them.
FRONT = contract_id_for(1001)
NEXT = contract_id_for(2002)

CAL = FeatureEngine(NQ).calendar


def session(day_offset, contract, base=20000, sweep_to=None, n=45):
    """One RTH session on one contract.

    Shape is fixed: a flat body, an optional sweep bar at minute 20 that
    wicks above `sweep_to` and closes back inside, and a bar at minute
    30 that closes above the opening range. So every session carries at
    most one prior-day sweep and exactly one opening-range break, which
    is what lets one session test both sides of the rule at once.
    """
    origin = RTH_OPEN + timedelta(days=day_offset)
    out = []
    for i in range(n):
        if sweep_to is not None and i == 20:
            o, h, l, c = base + 2, sweep_to, base, base + 2
        elif i == 30:
            o, h, l, c = base + 2, base + 9, base, base + 8
        else:
            o, h, l, c = base + 2, base + 4, base, base + 2
        out.append(bar(i, o, h, l, c, origin=origin, contract_id=contract))
    return out


def roll_bars():
    """Three sessions: two on the front contract, then a roll.

    01-12 front, 01-13 front (sweeps 01-12's high), 01-14 NEXT (sweeps
    01-13's high -- across the roll, and therefore inadmissible).
    """
    return (session(0, FRONT)
            + session(1, FRONT, sweep_to=20050)
            + session(2, NEXT, sweep_to=20090))


def run_engine(bars):
    engine = FeatureEngine(NQ)
    return engine.run(bars)


# --- provenance comes from symbology, not from a symbol or a date -----

def test_contract_id_is_the_instrument_id_not_the_continuous_symbol():
    """NQ.c.0 names a series, not a contract. Two contracts inside that
    series must be distinguishable, and the symbol cannot do it."""
    assert contract_id_for(1001) != contract_id_for(2002)
    assert "1001" in contract_id_for(1001)
    assert contract_id_for(1001).startswith("GLBX.MDP3:")
    # Unique only within a dataset, so the dataset is part of the name.
    assert contract_id_for(1001, "OTHER.DS") != contract_id_for(1001)


class _Rec:
    """The shape of a DBN OhlcvMsg, with nothing else on it."""
    def __init__(self, instrument_id, ts_event, px):
        self.instrument_id = instrument_id
        self.ts_event = ts_event
        scaled = int(px * 1_000_000_000)
        self.open = self.high = self.low = self.close = scaled
        self.high = scaled + 1_000_000_000
        self.low = scaled - 1_000_000_000
        self.volume = 10


def test_adapter_carries_the_contract_through_a_continuous_symbol():
    """The whole point of the rule: same symbol, two contracts."""
    from datetime import datetime, timezone
    captured = datetime(2026, 1, 20, tzinfo=timezone.utc)
    base_ns = int(datetime(2026, 1, 12, 15, 0,
                           tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    cont = Instrument(symbol="NQ.c.0", product="NQ", tick_size=Decimal("0.25"))
    src = DatabentoFileSource("unused", {"NQ.c.0": cont}, captured)
    records = [_Rec(1001, base_ns, 20000),
               _Rec(2002, base_ns + 60_000_000_000, 20500)]
    resolver = DictResolver({1001: "NQ.c.0", 2002: "NQ.c.0"})
    bars = src.bars_by_symbol(records=records, resolver=resolver)["NQ.c.0"]
    assert [b.instrument.symbol for b in bars] == ["NQ.c.0", "NQ.c.0"]
    assert [b.contract_id for b in bars] == [contract_id_for(1001),
                                             contract_id_for(2002)]


def test_normalize_derives_the_contract_when_none_is_supplied():
    from datetime import datetime, timezone
    captured = datetime(2026, 1, 20, tzinfo=timezone.utc)
    ts = int(datetime(2026, 1, 12, 15, 0,
                      tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    b = normalize_ohlcv(_Rec(4242, ts, 20000), NQ, captured)
    assert b.contract_id == contract_id_for(4242)


def test_the_rule_is_not_a_calendar_heuristic():
    """The operator's binding constraint, checked against the parsed
    code rather than the text, so prose explaining what a roll IS
    cannot be mistaken for code that infers one.

    A calendar heuristic -- "the day after the third Friday of a
    quarterly month" -- is right about a typical year and wrong about
    every early, late, staggered or holiday-shifted roll. Neither
    module may import the expiry calendar, and neither may look at the
    month or weekday of a date."""
    import ast
    for module in ("trading_system/features/contracts.py",
                   "trading_system/research/eligibility.py"):
        tree = ast.parse((ROOT / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "roll" not in (node.module or ""), \
                    f"{module} imports the expiry calendar"
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert "roll" not in a.name and "calendar" not in a.name, \
                        f"{module} imports {a.name}"
            # date.month / date.weekday() / date.day are how a calendar
            # heuristic is spelled. The rule compares contract ids and
            # orders sessions; it never asks what day it is.
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("month", "weekday", "isoweekday",
                                         "third_friday", "year"), \
                    f"{module} inspects date.{node.attr}"
            if isinstance(node, ast.Name):
                assert "expiry" not in node.id and "expiries" not in node.id, \
                    f"{module} reasons about expiries"


# --- the timeline -----------------------------------------------------

def test_timeline_reads_contracts_off_the_bars():
    tl = ContractTimeline.from_bars(roll_bars(), CAL)
    assert tl.contract_on(date(2026, 1, 12)).sole_contract == FRONT
    assert tl.contract_on(date(2026, 1, 14)).sole_contract == NEXT
    assert tl.summary()["distinct_contracts"] == 2


def test_the_four_ways_continuity_can_fail_are_distinguished():
    tl = ContractTimeline.from_bars(roll_bars(), CAL)
    assert tl.continuity(date(2026, 1, 12)).verdict is \
        ContinuityVerdict.NO_PRIOR_SESSION
    assert tl.continuity(date(2026, 1, 13)).verdict is \
        ContinuityVerdict.SAME_CONTRACT
    assert tl.continuity(date(2026, 1, 14)).verdict is \
        ContinuityVerdict.CONTRACT_BOUNDARY

    mixed = ContractTimeline({
        date(2026, 1, 12): SessionContract(date(2026, 1, 12), (FRONT,), 10, 0),
        date(2026, 1, 13): SessionContract(date(2026, 1, 13),
                                           (FRONT, NEXT), 10, 0),
    })
    assert mixed.continuity(date(2026, 1, 13)).verdict is \
        ContinuityVerdict.MIXED_SESSION

    blind = ContractTimeline.from_bars(
        [bar(i, 20000, 20004, 20000, 20002) for i in range(5)], CAL)
    assert blind.continuity(date(2026, 1, 12)).verdict is \
        ContinuityVerdict.UNKNOWN_PROVENANCE


def test_only_same_contract_is_eligible():
    for verdict in ContinuityVerdict:
        assert verdict.eligible is (verdict is ContinuityVerdict.SAME_CONTRACT)


def test_prior_session_means_the_previous_session_in_the_sample():
    """After the quality gate removes a day, the level carried forward
    comes from the previous day that PASSED. A timeline that compared
    against the previous CALENDAR day would certify a comparison the
    engine never made."""
    tl = ContractTimeline({
        date(2026, 1, 12): SessionContract(date(2026, 1, 12), (FRONT,), 10, 0),
        # 01-13 gated out, so it is simply absent
        date(2026, 1, 14): SessionContract(date(2026, 1, 14), (NEXT,), 10, 0),
    })
    assert tl.prior_session(date(2026, 1, 14)) == date(2026, 1, 12)
    c = tl.continuity(date(2026, 1, 14))
    assert c.prior_session_date == date(2026, 1, 12)
    assert c.verdict is ContinuityVerdict.CONTRACT_BOUNDARY


def test_a_session_without_full_provenance_is_never_certified():
    """Half-known is not known. One bar with no contract id is enough to
    refuse, because the missing one could be the other contract."""
    bars = session(0, FRONT) + [bar(0, 20000, 20004, 20000, 20002,
                                    origin=RTH_OPEN + timedelta(days=1))]
    tl = ContractTimeline.from_bars(bars, CAL)
    assert not tl.contract_on(date(2026, 1, 13)).has_provenance
    assert tl.continuity(date(2026, 1, 13)).verdict is \
        ContinuityVerdict.UNKNOWN_PROVENANCE


# --- which hypotheses the rule applies to, derived rather than listed --

def test_applicability_is_derived_from_the_primitives_not_a_hardcoded_list():
    src = (ROOT / "trading_system" / "research" / "eligibility.py").read_text()
    for hid in ("H6", "H7", "H1", "H12"):
        assert f'"{hid}"' not in src and f"'{hid}'" not in src, \
            f"{hid} is named in the rule; it must be derived"


def test_exactly_the_prior_session_hypotheses_are_affected():
    """Derived over the frozen registry. H6 and H7 are the sweeps; the
    other ten read only the current session's tape."""
    registry = build_registry()
    affected = {h.id for h in registry.all()
                if depends_on_prior_session_levels(h)}
    assert affected == {"H6", "H7"}


def test_a_future_hypothesis_conditioned_on_a_prior_day_level_is_caught():
    """The reason applicability is derived: nobody has to remember."""
    r = HypothesisRegistry()
    h = r.register(
        "HX", "an intraday event, conditioned on distance to yesterday's high",
        EventType.DISPLACEMENT_UP.value, Direction.UP, 30,
        {FeatureType.DISTANCE_TO_PRIOR_DAY_HIGH.value: "near"},
        "the confidence interval includes zero")
    assert depends_on_prior_session_levels(h)


def test_the_prior_session_primitive_sets_match_what_the_engine_carries():
    """Guards the derivation at its root: if the engine ever carries a
    fifth level across a session, this fails until it is declared."""
    src = (ROOT / "trading_system" / "features" / "engine.py").read_text()
    body = src.split("def _roll_session")[1].split("def ")[0]
    carried = {"_prior_day_high", "_prior_day_low",
               "_prior_overnight_high", "_prior_overnight_low"}
    assigned = {line.split("=")[0].strip().replace("self.", "")
                for line in body.splitlines()
                if "self._prior" in line and "=" in line}
    assert assigned == carried, f"engine now carries {assigned}"
    assert PRIOR_SESSION_EVENTS == {EventType.LIQUIDITY_SWEEP_HIGH.value,
                                    EventType.LIQUIDITY_SWEEP_LOW.value}
    assert len(PRIOR_SESSION_FEATURES) == 8


# --- the headline: a per-hypothesis exclusion, not a per-session one ---

def _study(contracts):
    # The partition is a label here: Study.test measures exactly the
    # events it is handed, so every event in the fixture is tested.
    days = [date(2026, 1, 12) + timedelta(days=i) for i in range(10)]
    registry = build_registry()
    return Study("t", split_chronologically(days), registry,
                 QualityReport(), contracts=contracts), registry


def _events_and_features(records):
    return ([r for r in records if r.kind.value == "event"],
            [r for r in records if r.kind.value == "feature"])


def test_sweeps_are_refused_across_the_roll_and_kept_within_a_contract():
    bars = roll_bars()
    records = run_engine(bars)
    events, features = _events_and_features(records)
    sweeps = [e for e in events
              if e.type == EventType.LIQUIDITY_SWEEP_HIGH.value]
    # The fixture is only meaningful if both sessions swept.
    assert {e.session_date for e in sweeps} == {"2026-01-13", "2026-01-14"}

    timeline = ContractTimeline.from_bars(bars, CAL)
    registry = build_registry()
    eligible, report = filter_eligible(registry.get("H6"), sweeps, timeline)

    assert [e.session_date for e in eligible] == ["2026-01-13"]
    assert report.applies is True
    assert report.rule == EXCLUSION_REASON
    assert report.events_excluded == 1
    assert report.excluded_by_verdict == {
        ContinuityVerdict.CONTRACT_BOUNDARY.value: 1}
    assert report.sessions_excluded == ["2026-01-14"]


def test_the_roll_session_still_serves_every_intraday_hypothesis():
    """Requirement 3 and 4 together: the session is not thrown away.
    Its opening-range break is a sound observation of H1 and stays."""
    bars = roll_bars()
    events, _ = _events_and_features(run_engine(bars))
    breaks = [e for e in events
              if e.type == EventType.OPENING_RANGE_HIGH_BROKEN.value]
    assert "2026-01-14" in {e.session_date for e in breaks}

    timeline = ContractTimeline.from_bars(bars, CAL)
    registry = build_registry()
    eligible, report = filter_eligible(registry.get("H1"), breaks, timeline)
    assert report.applies is False
    assert len(eligible) == len(breaks)
    assert report.events_excluded == 0
    assert "2026-01-14" in {e.session_date for e in eligible}


def test_the_study_splits_the_same_session_between_h6_and_h1():
    """The two previous tests, through the real code path, on one
    session: H6 loses the roll session, H1 keeps it."""
    bars = roll_bars()
    events, features = _events_and_features(run_engine(bars))
    timeline = ContractTimeline.from_bars(bars, CAL)
    study, _ = _study(timeline)

    h6 = study.test("H6", Partition.DISCOVERY, events, features, bars)
    h1 = study.test("H1", Partition.DISCOVERY, events, features, bars)

    assert h6.n_events == 1 and h6.eligibility.events_excluded == 1
    assert h6.eligibility.sessions_excluded == ["2026-01-14"]
    assert h1.eligibility.applies is False and h1.eligibility.events_excluded == 0
    assert h1.n_events == 3          # one per session, roll session included


def test_the_exclusion_reason_is_recorded_by_name_in_the_report():
    """Requirement 5. A shrunken sample must carry the reason it shrank
    into the persisted report, not just into a console line."""
    bars = roll_bars()
    events, features = _events_and_features(run_engine(bars))
    study, _ = _study(ContractTimeline.from_bars(bars, CAL))
    study.test("H6", Partition.DISCOVERY, events, features, bars)
    row = study.provenance()["results"][0]["eligibility"]
    assert row["rule"] == "contract_boundary_invalidation"
    assert row["applies"] is True
    assert row["excluded_by_verdict"] == {"contract_boundary": 1}
    assert row["sessions_excluded"] == ["2026-01-14"]
    assert study.provenance()["contract_provenance"]["distinct_contracts"] == 2


def test_a_study_without_provenance_refuses_every_prior_session_claim():
    """Fail closed. No timeline is not 'probably one contract'."""
    bars = roll_bars()
    events, features = _events_and_features(run_engine(bars))
    study, _ = _study(None)
    h6 = study.test("H6", Partition.DISCOVERY, events, features, bars)
    h1 = study.test("H1", Partition.DISCOVERY, events, features, bars)
    assert h6.n_events == 0
    assert h6.eligibility.excluded_by_verdict == {"unknown_provenance": 2}
    assert h1.n_events == 3, "an intraday claim needs no contract provenance"


def test_an_empty_sample_is_inconclusive_never_a_rejection():
    """A hypothesis the rule silences has not been refuted by anything."""
    bars = roll_bars()
    events, features = _events_and_features(run_engine(bars))
    study, _ = _study(None)
    for h in build_registry().all():
        study.test(h.id, Partition.DISCOVERY, events, features, bars)
    verdicts = study.classify_all()
    assert verdicts["H6"].verdict.value == "inconclusive"
    assert verdicts["H7"].verdict.value == "inconclusive"


def test_the_frozen_hypotheses_are_untouched_by_all_of_this():
    """Requirement 7. The rule changes which observations are admitted,
    never what any hypothesis claims."""
    registry = build_registry()
    assert registry.count() == 12
    assert registry.verify() and registry.sealed
    h6, h7 = registry.get("H6"), registry.get("H7")
    assert h6.event_type == EventType.LIQUIDITY_SWEEP_HIGH.value
    assert h6.direction is Direction.DOWN and h6.horizon_minutes == 60
    assert h7.event_type == EventType.LIQUIDITY_SWEEP_LOW.value
    assert h7.direction is Direction.UP and h7.horizon_minutes == 60


def test_the_holdout_is_not_touched_by_the_rule():
    src = (ROOT / "trading_system" / "research" / "eligibility.py").read_text()
    assert "holdout" not in src.lower()


# --- the runner must actually wire both fixes in -----------------------
# Neither failure is loud. A missing timeline makes H6/H7 report zero
# eligible events, and a per-day engine makes them report zero events at
# all -- both look like a research finding rather than a wiring bug.

RUNNER = ROOT / "scripts" / "run_t004_study.py"


def test_the_runner_hands_the_study_a_contract_timeline():
    import ast
    tree = ast.parse(RUNNER.read_text())
    built = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "from_bars"
                for n in ast.walk(tree))
    assert built, "the runner never builds a ContractTimeline"
    passed = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "Study"]
    assert passed, "no Study is constructed"
    assert any(kw.arg == "contracts" for call in passed for kw in call.keywords), \
        "Study is built without contract provenance; H6/H7 would silently " \
        "report zero eligible events"


def test_the_runner_feeds_one_engine_the_whole_stage():
    """A fresh FeatureEngine per day never carries a prior-day level
    forward, so _detect_sweeps cannot fire and H6/H7 have exactly zero
    events -- a silent floor with no error anywhere."""
    import ast
    tree = ast.parse(RUNNER.read_text())
    # The loop that walks one stage's sessions: `for day in days:`.
    day_loops = [n for n in ast.walk(tree)
                 if isinstance(n, ast.For)
                 and isinstance(n.target, ast.Name) and n.target.id == "day"
                 and isinstance(n.iter, ast.Name) and n.iter.id == "days"]
    assert day_loops, "the runner no longer loops over a stage's days"
    for loop in day_loops:
        constructs = [n for n in ast.walk(loop)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name)
                      and n.func.id == "FeatureEngine"]
        assert not constructs, \
            "a FeatureEngine is constructed inside the per-day loop, so no " \
            "prior-day level is ever carried forward and H6/H7 see nothing"


def test_a_fresh_engine_per_day_would_emit_no_sweeps_at_all():
    """Pins the defect itself, so the guard above cannot be relaxed on
    the belief that it was only stylistic."""
    bars_by_day = [session(0, FRONT), session(1, FRONT, sweep_to=20050),
                   session(2, NEXT, sweep_to=20090)]
    per_day = [r for day in bars_by_day
               for r in FeatureEngine(NQ).run(day)
               if r.type == EventType.LIQUIDITY_SWEEP_HIGH.value]
    assert per_day == []
    continuous = [r for r in run_engine(roll_bars())
                  if r.type == EventType.LIQUIDITY_SWEEP_HIGH.value]
    assert len(continuous) == 2
