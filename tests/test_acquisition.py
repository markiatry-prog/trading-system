"""The acquisition guards and the Databento adapter.

These protect two things that cannot be un-done: spending money on the
wrong request, and computing on data whose provenance is unverified.
"""
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_system.market_data import Instrument, MarketDataError  # noqa: E402
from trading_system.research.preregistered import build_registry  # noqa: E402
from trading_system.sources.databento_source import (  # noqa: E402
    DATASET, SCHEMA, STYPE_IN, DatabentoFileSource, normalize_ohlcv,
    ns_to_datetime)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ACQ = _load("acquire_t004_dataset")
NQ = Instrument(symbol="NQ.c.0", product="NQ", tick_size=Decimal("0.25"))


class FakeRecord:
    ts_event = 1757342400000000000
    open = 20000250000000
    high = 20010000000000
    low = 19995000000000
    close = 20005500000000
    volume = 1234


# --- the approved request ---------------------------------------------

def test_the_encoded_approval_matches_what_was_authorised():
    a = ACQ.APPROVED
    assert a["dataset"] == "GLBX.MDP3"
    assert a["schema"] == "ohlcv-1m"
    assert sorted(a["symbols"]) == ["ES.c.0", "MNQ.c.0", "NQ.c.0"]
    assert a["start"] == "2021-09-01" and a["end"] == "2026-09-01"
    assert a["approved_cost_usd"] == 19.25


def test_the_approved_request_revalidates():
    spec = {k: ACQ.APPROVED[k] for k in
            ("dataset", "schema", "symbols", "stype_in", "start", "end")}
    ACQ.revalidate(spec)   # must not raise


@pytest.mark.parametrize("field,value", [
    ("dataset", "XNAS.ITCH"),
    ("schema", "mbo"),
    ("schema", "trades"),
    ("schema", "mbp-10"),
    ("schema", "bbo-1s"),
    ("stype_in", "raw_symbol"),
    ("start", "2019-01-01"),
    ("end", "2027-01-01"),
])
def test_any_deviation_from_the_approval_is_refused(field, value):
    """Depth, tick data, extra range -- each explicitly excluded."""
    spec = {k: ACQ.APPROVED[k] for k in
            ("dataset", "schema", "symbols", "stype_in", "start", "end")}
    spec[field] = value
    with pytest.raises(ACQ.AcquisitionRefused):
        ACQ.revalidate(spec)


def test_adding_an_instrument_is_refused():
    spec = {k: ACQ.APPROVED[k] for k in
            ("dataset", "schema", "symbols", "stype_in", "start", "end")}
    spec["symbols"] = list(spec["symbols"]) + ["RTY.c.0"]
    with pytest.raises(ACQ.AcquisitionRefused, match="symbols"):
        ACQ.revalidate(spec)


# --- cost control -----------------------------------------------------

def test_the_approved_cost_passes():
    ACQ.check_cost(19.25)
    ACQ.check_cost(19.00)


def test_a_materially_higher_price_stops_the_acquisition():
    """The operator said stop and report. This is that, mechanically."""
    with pytest.raises(ACQ.AcquisitionRefused, match="above the approved"):
        ACQ.check_cost(19.25 * 1.11)


def test_small_rate_drift_is_tolerated():
    ACQ.check_cost(19.25 * 1.05)


def test_the_hard_ceiling_binds_independently_of_the_tolerance():
    """Even if someone widened the tolerance, one pull may never take
    more than a third of the credit."""
    original = ACQ.COST_TOLERANCE
    try:
        ACQ.COST_TOLERANCE = 100.0      # absurdly permissive
        with pytest.raises(ACQ.AcquisitionRefused, match="hard ceiling"):
            ACQ.check_cost(60.0)
    finally:
        ACQ.COST_TOLERANCE = original


def test_request_digest_changes_with_any_field():
    base = {k: ACQ.APPROVED[k] for k in
            ("dataset", "schema", "symbols", "stype_in", "start", "end")}
    d0 = ACQ.request_digest(base)
    for field, value in (("schema", "trades"), ("end", "2027-01-01")):
        changed = dict(base); changed[field] = value
        assert ACQ.request_digest(changed) != d0


# --- adapter ----------------------------------------------------------

def test_fixed_point_prices_survive_as_exact_decimals():
    bar = normalize_ohlcv(FakeRecord(), NQ, datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert isinstance(bar.open, Decimal)
    assert bar.open == Decimal("20000.25")
    assert NQ.is_on_tick(bar.open)
    assert bar.volume == 1234


def test_ts_event_is_treated_as_the_bar_open():
    """Treating it as a close would shift every bar by its own interval
    and move every session boundary."""
    bar = normalize_ohlcv(FakeRecord(), NQ, datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert bar.observed_at == ns_to_datetime(FakeRecord.ts_event)
    assert bar.closed_at == bar.observed_at + __import__("datetime").timedelta(minutes=1)


def test_nanosecond_precision_is_not_lost_through_float():
    assert ns_to_datetime(1757342400123456000).microsecond == 123456


def test_the_adapter_is_importable_without_the_databento_library():
    """Normalisation must be testable with no network, no key and no
    vendor dependency -- otherwise it is only tested in production."""
    import trading_system.sources.databento_source as mod
    src = Path(mod.__file__).read_text()
    top_level = [ln for ln in src.splitlines()
                 if ln.startswith("import ") or ln.startswith("from ")]
    assert not any("databento" in ln for ln in top_level)


def test_source_refuses_a_schema_it_does_not_provide():
    from trading_system.market_data import MarketDataSchema
    s = DatabentoFileSource("x.dbn", {"NQ.c.0": NQ},
                            datetime(2026, 9, 8, tzinfo=timezone.utc))
    with pytest.raises(MarketDataError, match="ohlcv-1m"):
        list(s.history(NQ, MarketDataSchema.TRADES,
                       datetime(2020, 1, 1, tzinfo=timezone.utc),
                       datetime(2030, 1, 1, tzinfo=timezone.utc)))


# --- frozen hypothesis set --------------------------------------------

def test_the_preregistered_set_is_sealed_and_intact():
    r = build_registry()
    assert r.count() == 12
    assert r.sealed and r.verify()


def test_every_preregistered_hypothesis_has_a_falsifier_and_a_direction():
    for h in build_registry().all():
        assert len(h.falsifier) >= 15
        assert h.direction is not None
        assert h.horizon_minutes > 0


def test_the_frozen_set_cannot_be_extended_after_sealing():
    from trading_system.research.hypotheses import Direction, RegistryError
    r = build_registry()
    with pytest.raises(RegistryError, match="sealed"):
        r.register("H99", "found after looking", "e", Direction.UP, 30, {},
                   "a perfectly valid falsifier string")


# --- manifest integrity -----------------------------------------------

STUDY = _load("run_t004_study")


def test_missing_manifest_refuses_to_run(tmp_path):
    with pytest.raises(SystemExit, match="provenance is mandatory"):
        STUDY.verify_manifest(tmp_path)


def test_tampered_data_file_is_detected(tmp_path):
    payload = b"original bytes"
    (tmp_path / "d.dbn.zst").write_bytes(payload)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "file": "d.dbn.zst", "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "finished_at": "2026-09-08T00:00:00+00:00", "repriced_cost_usd": 19.25}))
    assert STUDY.verify_manifest(tmp_path)["sha256"]
    (tmp_path / "d.dbn.zst").write_bytes(b"tampered bytes")
    with pytest.raises(SystemExit, match="INTEGRITY FAILURE"):
        STUDY.verify_manifest(tmp_path)


# --- symbology: instrument separation ---------------------------------
#
# The defect chain this pins:
#   1st run: one instrument assigned to every record -> NQ, MNQ and ES
#            pooled into one series at three price levels.
#   2nd run: a resolver written against a GUESSED metadata shape
#            (`entry.raw_symbol` / `entry.intervals[].instrument_id`)
#            resolved nothing and failed closed.
# The real API is databento's InstrumentMap, whose `resolve` is keyed on
# (instrument_id, DATE) because a continuous symbol's instrument_id
# changes at every roll.

from datetime import date as _date  # noqa: E402

from trading_system.sources.databento_source import (  # noqa: E402
    DictResolver, InstrumentMapResolver, SymbologyError, build_symbology,
    is_symbol_mapping)

BASE_NS = 1757342400000000000          # 2025-09-08T14:40:00Z
MIN_NS = 60_000_000_000


class _Rec:
    def __init__(self, iid, ts, price):
        self.instrument_id = iid
        self.ts_event = ts
        self.open = self.high = self.low = self.close = price
        self.volume = 10


def _source(symbols=("NQ.c.0", "MNQ.c.0", "ES.c.0")):
    from trading_system.sources.databento_source import DatabentoFileSource
    instruments = {s: Instrument(symbol=s, product=s.split(".")[0],
                                 tick_size=Decimal("0.25")) for s in symbols}
    return DatabentoFileSource("f.dbn", instruments,
                               datetime(2026, 9, 8, tzinfo=timezone.utc))


def test_interleaved_instrument_ids_land_in_the_correct_streams():
    """Requirement 8: multiple interleaved IDs, each record in the right
    stream. Prices differ by instrument so a pooled series would be
    obvious -- but nothing here INFERS the symbol from price."""
    src = _source()
    records = [
        _Rec(1, BASE_NS, 20000_000000000),            # NQ
        _Rec(2, BASE_NS, 20000_000000000),            # MNQ, same price
        _Rec(3, BASE_NS, 5600_000000000),             # ES
        _Rec(3, BASE_NS + MIN_NS, 5601_000000000),
        _Rec(1, BASE_NS + MIN_NS, 20001_000000000),
        _Rec(2, BASE_NS + MIN_NS, 20001_000000000),
        _Rec(1, BASE_NS + 2 * MIN_NS, 20002_000000000),
    ]
    resolver = DictResolver({1: "NQ.c.0", 2: "MNQ.c.0", 3: "ES.c.0"})
    out = src.bars_by_symbol(records=records, resolver=resolver)
    assert [len(out[s]) for s in ("NQ.c.0", "MNQ.c.0", "ES.c.0")] == [3, 2, 2]
    for symbol, bars in out.items():
        assert all(b.instrument.symbol == symbol for b in bars)
    assert out["ES.c.0"][0].close == Decimal("5600")
    assert out["NQ.c.0"][0].close == Decimal("20000")
    # requirement 10, in miniature: nothing lost, nothing duplicated
    assert sum(len(v) for v in out.values()) == len(records)


def test_the_same_instrument_id_can_map_to_different_symbols_over_time():
    """A continuous symbol rolls: one instrument_id is NQ.c.0 for a while
    and something else later. A flat dict is right until the first roll
    and wrong afterwards, showing up as a price discontinuity rather
    than an error."""
    class RollingResolver:
        def resolve(self, iid, on):
            if int(iid) != 7:
                return None
            return "NQ.c.0" if on < _date(2025, 9, 9) else "MNQ.c.0"
        def observe(self, record):
            return None

    src = _source()
    day_two = BASE_NS + 24 * 60 * MIN_NS
    out = src.bars_by_symbol(
        records=[_Rec(7, BASE_NS, 20000_000000000),
                 _Rec(7, day_two, 20050_000000000)],
        resolver=RollingResolver())
    assert len(out["NQ.c.0"]) == 1 and len(out["MNQ.c.0"]) == 1


def test_unresolvable_ids_fail_closed_and_are_never_dropped():
    """Requirements 6 and 7."""
    src = _source()
    records = [_Rec(1, BASE_NS, 20000_000000000),
               _Rec(99, BASE_NS, 20000_000000000)]
    with pytest.raises(SymbologyError, match="could not be resolved"):
        src.bars_by_symbol(records=records,
                           resolver=DictResolver({1: "NQ.c.0"}))


def test_a_resolved_symbol_outside_the_requested_set_is_refused():
    src = _source()
    with pytest.raises(MarketDataError, match="not in this source"):
        src.bars_by_symbol(records=[_Rec(4, BASE_NS, 100_000000000)],
                           resolver=DictResolver({4: "RTY.c.0"}))


def test_backwards_time_within_one_symbol_is_refused():
    src = _source()
    with pytest.raises(MarketDataError, match="backwards in time"):
        src.bars_by_symbol(
            records=[_Rec(1, BASE_NS + MIN_NS, 20001_000000000),
                     _Rec(1, BASE_NS, 20000_000000000)],
            resolver=DictResolver({1: "NQ.c.0"}))


def test_interleaving_is_not_mistaken_for_backwards_time():
    """Order holds within each symbol; the file interleaves them."""
    src = _source()
    out = src.bars_by_symbol(
        records=[_Rec(1, BASE_NS + MIN_NS, 20001_000000000),
                 _Rec(3, BASE_NS, 5600_000000000)],
        resolver=DictResolver({1: "NQ.c.0", 3: "ES.c.0"}))
    assert len(out["NQ.c.0"]) == 1 and len(out["ES.c.0"]) == 1


def test_symbol_mapping_messages_are_fed_to_the_resolver_not_parsed_as_bars():
    class SymbolMappingMsg:                      # name is what is matched
        instrument_id = 1
    seen = []

    class Recording(DictResolver):
        def observe(self, record):
            seen.append(record)

    src = _source()
    out = src.bars_by_symbol(
        records=[SymbolMappingMsg(), _Rec(1, BASE_NS, 20000_000000000)],
        resolver=Recording({1: "NQ.c.0"}))
    assert len(seen) == 1
    assert len(out["NQ.c.0"]) == 1


def test_is_symbol_mapping_matches_by_type_name():
    class SymbolMappingMsg:
        pass
    assert is_symbol_mapping(SymbolMappingMsg())
    assert not is_symbol_mapping(_Rec(1, BASE_NS, 1))


def test_instrument_map_resolver_returns_none_rather_than_raising():
    """A miss must be a clean None so the caller can fail closed with a
    useful count, not an opaque exception from inside the vendor."""
    class Raises:
        def resolve(self, iid, on):
            raise ValueError("no mapping")
    assert InstrumentMapResolver(Raises()).resolve(1, _date(2025, 1, 1)) is None


def test_build_symbology_refuses_a_file_without_metadata():
    class NoMeta:
        metadata = None
    with pytest.raises(SymbologyError, match="no metadata"):
        build_symbology(NoMeta())


def test_no_instrument_id_is_hard_coded_anywhere():
    """Requirement 3. Symbol identity comes from the file, never from a
    literal in our source."""
    import re
    src = (ROOT / "trading_system" / "sources" / "databento_source.py").read_text()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    assert "instrument_id ==" not in code
    assert not re.search(r"\{\s*\d+\s*:\s*[\"']", code), "an id->symbol literal"


def test_symbols_are_never_inferred_from_price():
    """Requirement 4."""
    src = (ROOT / "trading_system" / "sources" / "databento_source.py").read_text()
    for banned in ("if price", "close >", "close <", "price >", "price <"):
        assert banned not in src, banned


# --- real-file smoke test (requirements 9 and 10) ---------------------

DATA = ROOT / "data" / "t004"


@pytest.mark.skipif(not (DATA / "manifest.json").exists(),
                    reason="acquired dataset not present in this environment")
def test_real_file_resolves_into_exactly_the_three_continuous_symbols():
    """Runs only where the acquired file exists. Skipped in CI, which
    holds no market data by design."""
    from trading_system.sources.databento_source import DatabentoFileSource
    manifest = json.loads((DATA / "manifest.json").read_text())
    instruments = {s: Instrument(symbol=s, product=s.split(".")[0],
                                 tick_size=Decimal("0.25"))
                   for s in ("NQ.c.0", "MNQ.c.0", "ES.c.0")}
    src = DatabentoFileSource(DATA / manifest["file"], instruments,
                              datetime.now(timezone.utc))
    out = src.bars_by_symbol()

    assert set(out) == {"NQ.c.0", "MNQ.c.0", "ES.c.0"}
    for symbol, bars in out.items():
        assert bars, f"{symbol} resolved to zero bars"
        span_days = (bars[-1].observed_at - bars[0].observed_at).days or 1
        sessions = max(1, span_days * 5 / 7)
        per_session = len(bars) / sessions
        assert per_session <= 1440, (
            f"{symbol}: {per_session:,.0f} bars per session exceeds the "
            f"1,440 minutes a day contains; instruments are pooled")
        assert per_session > 100, f"{symbol}: only {per_session:,.0f} per session"
    total = sum(len(v) for v in out.values())
    assert total == src.last_record_total, (
        f"{total:,} bars kept vs {src.last_record_total:,} records read; "
        f"records were lost or duplicated")
