"""The canonical contract is the seam the whole system depends on: if it
admits a bad record, every downstream feature inherits the error, and a
backtest built on it is wrong in a way no later test can detect. So the
rejections are tested as carefully as the acceptances."""
import ast
import csv
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trading_system.market_data import (  # noqa: E402
    MAX_CLOCK_SKEW, Bar, Instrument, MarketDataError, MarketDataSchema,
    Side, TopOfBook, Trade, price_from_fixed, records_digest,
)
from trading_system.sources.replay import CsvReplaySource, parse_observed_at  # noqa: E402

NQ = Instrument(symbol="NQZ6", product="NQ", tick_size=Decimal("0.25"))
T0 = datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc)


def _trade(**kw):
    base = dict(instrument=NQ, price=Decimal("20000.25"), size=1,
                observed_at=T0, captured_at=T0)
    base.update(kw)
    return Trade(**base)


# --- prices -----------------------------------------------------------

def test_price_from_fixed_is_exact_at_databento_scale():
    """1e-9 fixed point is what Databento publishes. Through float this
    lands on 20000.249999999996 and an on-tick test starts failing."""
    assert price_from_fixed(20_000_250_000_000) == Decimal("20000.25")
    assert NQ.is_on_tick(price_from_fixed(20_000_250_000_000))


def test_float_prices_are_refused():
    with pytest.raises(MarketDataError, match="Decimal"):
        _trade(price=20000.25)


def test_on_tick_detects_an_off_tick_price():
    assert NQ.is_on_tick(Decimal("20000.25"))
    assert not NQ.is_on_tick(Decimal("20000.30"))


# --- timestamps -------------------------------------------------------

def test_naive_timestamps_are_refused():
    with pytest.raises(MarketDataError, match="timezone-aware"):
        _trade(observed_at=datetime(2026, 9, 1, 13, 30))


def test_capture_far_before_the_event_is_refused():
    """Small skew is normal; an hour is a parse bug."""
    with pytest.raises(MarketDataError, match="ahead of"):
        _trade(captured_at=T0 - timedelta(hours=1))


def test_ordinary_feed_latency_is_accepted():
    assert _trade(captured_at=T0 + timedelta(milliseconds=40)).size == 1
    assert _trade(captured_at=T0 - MAX_CLOCK_SKEW + timedelta(seconds=1)).size == 1


def test_zero_or_negative_trade_size_is_refused():
    for bad in (0, -1):
        with pytest.raises(MarketDataError, match="size"):
            _trade(size=bad)


# --- top of book ------------------------------------------------------

def test_crossed_book_is_reported_not_rejected():
    """CME's cloud feed conflates top of book at 500ms and genuinely
    emits crossed quotes. Rejecting them would force adapters to drop
    real data silently."""
    tob = TopOfBook(instrument=NQ, bid_price=Decimal("20001"), bid_size=1,
                    ask_price=Decimal("20000"), ask_size=1,
                    observed_at=T0, captured_at=T0)
    assert tob.is_crossed


def test_one_sided_book_has_no_mid_and_is_not_crossed():
    tob = TopOfBook(instrument=NQ, bid_price=None, bid_size=None,
                    ask_price=Decimal("20000"), ask_size=3,
                    observed_at=T0, captured_at=T0)
    assert tob.mid is None and not tob.is_crossed


def test_mid_is_exact():
    tob = TopOfBook(instrument=NQ, bid_price=Decimal("20000.00"), bid_size=1,
                    ask_price=Decimal("20000.50"), ask_size=1,
                    observed_at=T0, captured_at=T0)
    assert tob.mid == Decimal("20000.25")


# --- bars -------------------------------------------------------------

def _bar(**kw):
    base = dict(instrument=NQ, interval_seconds=60, open=Decimal("100"),
                high=Decimal("110"), low=Decimal("90"), close=Decimal("105"),
                volume=10, observed_at=T0, captured_at=T0)
    base.update(kw)
    return Bar(**base)


def test_bar_ohlc_inconsistency_is_refused():
    with pytest.raises(MarketDataError, match="exceeds high"):
        _bar(low=Decimal("120"))
    with pytest.raises(MarketDataError, match="outside"):
        _bar(close=Decimal("200"))
    with pytest.raises(MarketDataError, match="outside"):
        _bar(open=Decimal("50"))


def test_bar_closed_at_is_derived_from_the_interval():
    assert _bar(interval_seconds=300).closed_at == T0 + timedelta(minutes=5)


def test_negative_volume_is_refused():
    with pytest.raises(MarketDataError, match="volume"):
        _bar(volume=-1)


# --- provenance -------------------------------------------------------

def test_digest_is_provider_independent():
    """The point of the canonical digest: the same market window from two
    vendors must hash identically, so a provider swap that changed the
    data underneath a backtest is detectable."""
    a = _trade(provider="databento")
    b = _trade(provider="replay:file.csv", captured_at=T0 + timedelta(seconds=9))
    assert records_digest([a]) == records_digest([b])


def test_digest_changes_when_the_market_data_changes():
    assert records_digest([_trade()]) != records_digest([_trade(size=2)])
    assert records_digest([_trade()]) != records_digest([_trade(price=Decimal("20000.50"))])


# --- replay source ----------------------------------------------------

def _write(tmp_path, rows, header):
    path = tmp_path / "data.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    return path


def test_replay_yields_trades_in_window(tmp_path):
    rows = [
        {"observed_at": "2026-09-01T13:30:00Z", "price": "20000.25", "size": "2", "aggressor": "ask"},
        {"observed_at": "2026-09-01T13:30:01Z", "price": "20000.50", "size": "1", "aggressor": "bid"},
        {"observed_at": "2026-09-01T14:00:00Z", "price": "20010.00", "size": "5", "aggressor": "none"},
    ]
    path = _write(tmp_path, rows, ["observed_at", "price", "size", "aggressor"])
    src = CsvReplaySource(path, NQ)
    got = list(src.history(NQ, MarketDataSchema.TRADES, T0, T0 + timedelta(minutes=1)))
    assert [t.size for t in got] == [2, 1]
    assert got[0].aggressor is Side.ASK
    assert got[0].price == Decimal("20000.25")
    assert got[0].provider == src.name


def test_replay_refuses_a_naive_timestamp(tmp_path):
    path = _write(tmp_path, [{"observed_at": "2026-09-01T13:30:00", "price": "1", "size": "1"}],
                  ["observed_at", "price", "size"])
    with pytest.raises(MarketDataError, match="no UTC offset"):
        list(CsvReplaySource(path, NQ).history(NQ, MarketDataSchema.TRADES, T0, T0 + timedelta(days=1)))


def test_replay_refuses_out_of_order_rows(tmp_path):
    rows = [
        {"observed_at": "2026-09-01T13:30:05Z", "price": "1", "size": "1"},
        {"observed_at": "2026-09-01T13:30:01Z", "price": "1", "size": "1"},
    ]
    path = _write(tmp_path, rows, ["observed_at", "price", "size"])
    with pytest.raises(MarketDataError, match="backwards in time"):
        list(CsvReplaySource(path, NQ).history(NQ, MarketDataSchema.TRADES, T0, T0 + timedelta(days=1)))


def test_replay_reports_the_offending_line_number(tmp_path):
    rows = [
        {"observed_at": "2026-09-01T13:30:00Z", "price": "1", "size": "1"},
        {"observed_at": "2026-09-01T13:30:01Z", "price": "oops", "size": "1"},
    ]
    path = _write(tmp_path, rows, ["observed_at", "price", "size"])
    with pytest.raises(MarketDataError, match=r":3:"):
        list(CsvReplaySource(path, NQ).history(NQ, MarketDataSchema.TRADES, T0, T0 + timedelta(days=1)))


def test_replay_parses_bars_and_top_of_book(tmp_path):
    bars = _write(tmp_path, [{"observed_at": "2026-09-01T13:30:00Z", "open": "1", "high": "3",
                              "low": "0.5", "close": "2", "volume": "7"}],
                  ["observed_at", "open", "high", "low", "close", "volume"])
    (bar,) = list(CsvReplaySource(bars, NQ, interval_seconds=300)
                  .history(NQ, MarketDataSchema.BARS, T0, T0 + timedelta(days=1)))
    assert bar.close == Decimal("2") and bar.interval_seconds == 300


def test_parse_observed_at_accepts_offsets_other_than_z():
    assert parse_observed_at("2026-09-01T09:30:00-04:00") == T0


# --- structural: the contract must stay provider-neutral --------------

def _module_imports(path):
    tree = ast.parse(Path(path).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_the_contract_imports_no_vendor_and_no_network():
    """If the canonical contract ever imports a vendor SDK or a network
    library, it has stopped being canonical and the feature engine is
    coupled to whoever we happened to pick first."""
    imported = _module_imports(ROOT / "trading_system" / "market_data.py")
    forbidden = {"databento", "ibapi", "ib_insync", "tradovate", "requests",
                 "httpx", "urllib", "socket", "websocket", "websockets",
                 "aiohttp", "psycopg2"}
    assert not (imported & forbidden), imported & forbidden


def test_the_contract_has_no_order_or_position_concept():
    """The air gap, asserted at the type level rather than trusted."""
    source = (ROOT / "trading_system" / "market_data.py").read_text().lower()
    tree = ast.parse((ROOT / "trading_system" / "market_data.py").read_text())
    defined = {n.name.lower() for n in ast.walk(tree)
               if isinstance(n, (ast.ClassDef, ast.FunctionDef))}
    for banned in ("order", "position", "account", "fill", "execution"):
        assert not any(banned in name for name in defined), banned


def test_the_replay_adapter_touches_no_database():
    imported = _module_imports(ROOT / "trading_system" / "sources" / "replay.py")
    assert "psycopg2" not in imported
    assert "db" not in imported


def test_a_past_event_captured_now_is_fine_but_a_future_event_is_not():
    """The asymmetry is deliberate and was found by these tests dating a
    fixture one day into the future. Historical replay legitimately has
    observed_at long before captured_at -- that is what replay IS. The
    reverse, an event stamped after the moment we received it, means the
    feed clock or the parse is wrong, and it must not reach the feature
    engine."""
    now = datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc)
    long_ago = now - timedelta(days=365)
    assert _trade(observed_at=long_ago, captured_at=now).size == 1
    with pytest.raises(MarketDataError, match="ahead of"):
        _trade(observed_at=now + timedelta(days=1), captured_at=now)
