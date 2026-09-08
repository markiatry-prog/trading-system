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
    s = DatabentoFileSource("x.dbn", NQ, datetime(2026, 9, 8, tzinfo=timezone.utc))
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
