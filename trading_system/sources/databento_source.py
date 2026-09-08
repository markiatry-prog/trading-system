"""Databento adapter: DBN ohlcv-1m -> canonical Bar.

The ONLY module that knows Databento exists. Everything downstream sees
`trading_system.market_data.Bar` and could not tell which vendor produced
it -- which is the property that makes the provider replaceable.

DELIBERATELY NOT IMPORTING `databento` AT MODULE LEVEL. The normalisation
is a pure function of record-like objects, so it is unit-testable with no
network, no key and no dependency. The library is imported only inside
the fetch path, which the operator runs.

FIXED-POINT PRICES. Databento publishes 1e-9 scaled integers. They go
through `price_from_fixed`, which is Decimal throughout: via float,
20000.25 becomes 20000.249999999996 and every exact tick comparison
starts failing intermittently.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import (Callable, Dict, Iterable, Iterator, List, Mapping,
                    Optional, Sequence)

from ..market_data import (
    Bar, Instrument, MarketDataError, MarketDataSchema, price_from_fixed,
)

DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"
STYPE_IN = "continuous"
BAR_SECONDS = 60

# Databento timestamps are nanoseconds since the UNIX epoch, UTC.
NS_PER_SECOND = 1_000_000_000


def ns_to_datetime(ns: int) -> datetime:
    """Nanoseconds -> timezone-aware UTC, without losing the sub-second part.

    `datetime.fromtimestamp(ns / 1e9)` would round-trip through float and
    lose precision at nanosecond scale; the integer split does not.
    """
    seconds, remainder = divmod(int(ns), NS_PER_SECOND)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=remainder // 1000)


def normalize_ohlcv(record, instrument: Instrument, captured_at: datetime,
                    provider: str = "databento") -> Bar:
    """One DBN OhlcvMsg -> one canonical Bar.

    `ts_event` on a Databento OHLCV record is the bar's OPEN time, which
    matches `Bar.observed_at`'s contract exactly. Treating it as a close
    would shift every bar forward by its own interval and silently move
    every session boundary.
    """
    return Bar(
        instrument=instrument,
        interval_seconds=BAR_SECONDS,
        open=price_from_fixed(record.open),
        high=price_from_fixed(record.high),
        low=price_from_fixed(record.low),
        close=price_from_fixed(record.close),
        volume=int(record.volume),
        observed_at=ns_to_datetime(record.ts_event),
        captured_at=captured_at,
        provider=provider,
    )


def build_symbol_resolver(store) -> Callable[[int], str]:
    """instrument_id -> symbol, from the file's own symbology.

    THIS IS NOT OPTIONAL. A multi-symbol DBN file interleaves every
    requested instrument in one stream, distinguished ONLY by
    instrument_id. Assigning the caller's instrument to every record
    silently pools NQ, MNQ and ES into a single series at three
    different price levels -- which is exactly the defect that produced
    "5,272,572 bars" for one symbol when an ETH session holds 1,380
    minutes. Every level, VWAP and excursion computed from that would
    have been meaningless, and nothing downstream could have detected it.

    Several databento-python versions expose the mapping differently, so
    each is tried in turn and the failure is loud rather than falling
    back to a guess.
    """
    mapping = getattr(store, "symbology_map", None)
    if mapping:
        table = {int(k): str(v) for k, v in dict(mapping).items()}
        if table:
            return lambda iid: table.get(int(iid), "")

    metadata = getattr(store, "metadata", None)
    mappings = getattr(metadata, "mappings", None)
    if mappings:
        table = {}
        for entry in mappings:
            raw = getattr(entry, "raw_symbol", None) or getattr(entry, "symbol", None)
            for interval in getattr(entry, "intervals", []) or []:
                iid = getattr(interval, "instrument_id", None)
                if iid is not None and raw:
                    table[int(iid)] = str(raw)
        if table:
            return lambda iid: table.get(int(iid), "")

    symbols = list(getattr(metadata, "symbols", []) or [])
    if len(symbols) == 1:
        only = str(symbols[0])
        return lambda iid: only

    raise MarketDataError(
        "cannot resolve instrument_id to a symbol from this file. Refusing "
        "to guess: assigning one instrument to every record would pool "
        "different contracts into a single series and silently corrupt "
        "every level and excursion computed from it."
    )


class DatabentoFileSource:
    """Reads a previously downloaded DBN file. No network, no key.

    Acquisition and analysis are separated on purpose: the download
    happens once, and every subsequent run of the study reads the same
    local bytes. A study that re-downloaded its own inputs could not be
    reproduced, and would spend money each time it ran.

    `instruments` maps symbol -> Instrument, because one file holds all
    the symbols that were requested together.
    """

    def __init__(self, path, instruments: Mapping[str, Instrument],
                 captured_at: datetime, provider: str = "databento"):
        if not instruments:
            raise MarketDataError("at least one instrument must be supplied")
        self.path = path
        self.instruments = dict(instruments)
        self.captured_at = captured_at
        self.provider = provider
        self.name = f"databento:{','.join(sorted(self.instruments))}"

    def schemas(self) -> Sequence[MarketDataSchema]:
        return (MarketDataSchema.BARS,)

    def _open_store(self):
        try:
            import databento as db
        except ImportError:  # pragma: no cover - operator environment only
            raise MarketDataError(
                "databento is not installed; run: pip install databento"
            ) from None
        return db.DBNStore.from_file(self.path)

    def bars_by_symbol(self, records=None, resolver=None) -> Dict[str, List[Bar]]:
        """Every bar, split by the symbol it actually belongs to.

        `records` and `resolver` are injectable so this is unit-testable
        with no file, no network and no vendor library.
        """
        if records is None or resolver is None:
            store = self._open_store()
            records = iter(store)
            resolver = build_symbol_resolver(store)

        out: Dict[str, List[Bar]] = {sym: [] for sym in self.instruments}
        unknown: Dict[str, int] = {}
        last_seen: Dict[str, datetime] = {}

        for record in records:
            if not hasattr(record, "ts_event") or not hasattr(record, "open"):
                continue                      # metadata / symbol-mapping rows
            symbol = resolver(getattr(record, "instrument_id", -1))
            instrument = self.instruments.get(symbol)
            if instrument is None:
                unknown[symbol] = unknown.get(symbol, 0) + 1
                continue
            bar = normalize_ohlcv(record, instrument, self.captured_at,
                                  self.provider)
            previous = last_seen.get(symbol)
            if previous is not None and bar.observed_at < previous:
                raise MarketDataError(
                    f"{symbol} goes backwards in time at "
                    f"{bar.observed_at.isoformat()}; every rolling feature "
                    f"would be silently wrong")
            last_seen[symbol] = bar.observed_at
            out[symbol].append(bar)

        if unknown:
            raise MarketDataError(
                f"records resolved to symbols not in this source: "
                f"{dict(sorted(unknown.items()))}. Dropping them silently "
                f"would change the sample without saying so.")
        return out

    def history(self, instrument: Instrument, schema: MarketDataSchema,
                start: datetime, end: datetime) -> Iterable[Bar]:
        if schema is not MarketDataSchema.BARS:
            raise MarketDataError(
                f"this source provides {SCHEMA} bars only, not {schema.value}")
        if instrument.symbol not in self.instruments:
            raise MarketDataError(
                f"{instrument.symbol} is not one of this source's symbols")
        for bar in self.bars_by_symbol()[instrument.symbol]:
            if start <= bar.observed_at < end:
                yield bar
