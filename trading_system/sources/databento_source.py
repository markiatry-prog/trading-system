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

from datetime import date, datetime, timedelta, timezone
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


class SymbologyError(MarketDataError):
    """Raised when a record's instrument cannot be identified."""


class DictResolver:
    """Date-aware resolver backed by an explicit table. Used in tests, and
    a precise statement of the interface the real one implements."""

    def __init__(self, table: Mapping[int, str]):
        self._table = {int(k): str(v) for k, v in table.items()}

    def resolve(self, instrument_id: int, on: date) -> Optional[str]:
        return self._table.get(int(instrument_id))

    def observe(self, record) -> None:      # no symbol-mapping messages
        return None


class InstrumentMapResolver:
    """Wraps databento's own `InstrumentMap`.

    WHY DATE-AWARE RESOLUTION IS NOT OPTIONAL. A continuous symbol like
    NQ.c.0 is not one contract: it is whichever contract is front month
    on a given day, and the instrument_id therefore CHANGES at every
    quarterly roll. A flat instrument_id -> symbol dict is right until
    the first roll and wrong afterwards, in a way that shows up as a
    price discontinuity rather than an error.

    `metadata.mappings` is a dict keyed by the INPUT symbol, whose
    entries carry the output symbol plus a start/end date -- not a list
    of objects with `.raw_symbol` and `.intervals`, which is what the
    previous implementation guessed at and why it resolved nothing.
    """

    def __init__(self, instrument_map):
        self._map = instrument_map

    def resolve(self, instrument_id: int, on: date) -> Optional[str]:
        try:
            return self._map.resolve(int(instrument_id), on)
        except (ValueError, KeyError):
            return None

    def observe(self, record) -> None:
        """Feed a SymbolMappingMsg from the stream.

        Metadata alone can be incomplete; the stream also carries mapping
        messages, and DBNStore itself folds them in the same way.
        """
        try:
            self._map.insert_symbol_mapping_msg(record)
        except Exception:                    # noqa: BLE001
            pass


def build_symbology(store) -> InstrumentMapResolver:
    """Build the resolver from the file's own symbology. No guessing."""
    # Checked before the import: a file with no metadata is unusable
    # whether or not the library is present, and reporting the missing
    # dependency instead would send the reader down the wrong path.
    metadata = getattr(store, "metadata", None)
    if metadata is None:
        raise SymbologyError("this DBN file carries no metadata")
    try:
        from databento.common.symbology import InstrumentMap
    except ImportError:  # pragma: no cover - operator environment only
        raise SymbologyError(
            "databento is not installed; run: pip install databento"
        ) from None
    instrument_map = InstrumentMap()
    instrument_map.insert_metadata(metadata)
    return InstrumentMapResolver(instrument_map)


def is_symbol_mapping(record) -> bool:
    return type(record).__name__ == "SymbolMappingMsg"


class DatabentoFileSource:
    """Reads a previously downloaded DBN file. No network, no key.

    Acquisition and analysis are separated on purpose: the download
    happens once, and every subsequent run of the study reads the same
    local bytes. A study that re-downloaded its own inputs could not be
    reproduced, and would spend money each time it ran.

    `instruments` maps symbol -> Instrument, because one file holds every
    symbol that was requested together, separated only by instrument_id.
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
            resolver = build_symbology(store)
            records = iter(store)

        out: Dict[str, List[Bar]] = {sym: [] for sym in self.instruments}
        unknown: Dict[str, int] = {}
        unresolved = 0
        last_seen: Dict[str, datetime] = {}
        total = 0

        for record in records:
            if is_symbol_mapping(record):
                resolver.observe(record)
                continue
            if not hasattr(record, "ts_event") or not hasattr(record, "open"):
                continue                      # other metadata rows
            total += 1
            observed_at = ns_to_datetime(record.ts_event)
            symbol = resolver.resolve(getattr(record, "instrument_id", -1),
                                      observed_at.date())
            if symbol is None:
                unresolved += 1
                continue
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

        if unresolved:
            raise SymbologyError(
                f"{unresolved:,} of {total:,} records could not be resolved to "
                f"a symbol. Failing closed rather than dropping them: a "
                f"silently smaller sample is worse than no sample.")
        if unknown:
            raise MarketDataError(
                f"records resolved to symbols not in this source: "
                f"{dict(sorted(unknown.items()))}. Dropping them silently "
                f"would change the sample without saying so.")
        self.last_record_total = total
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
