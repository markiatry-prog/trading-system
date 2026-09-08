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
from typing import Callable, Iterable, Iterator, Optional, Sequence

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


class DatabentoFileSource:
    """Reads a previously downloaded DBN file. No network, no key.

    Acquisition and analysis are separated on purpose: the download
    happens once, and every subsequent run of the study reads the same
    local bytes. A study that re-downloaded its own inputs could not be
    reproduced, and would spend money each time it ran.
    """

    def __init__(self, path, instrument: Instrument, captured_at: datetime,
                 provider: str = "databento"):
        self.path = path
        self.instrument = instrument
        self.captured_at = captured_at
        self.provider = provider
        self.name = f"databento:{instrument.symbol}"

    def schemas(self) -> Sequence[MarketDataSchema]:
        return (MarketDataSchema.BARS,)

    def _records(self) -> Iterator:
        try:
            import databento as db
        except ImportError:  # pragma: no cover - operator environment only
            raise MarketDataError(
                "databento is not installed; run: pip install databento"
            ) from None
        store = db.DBNStore.from_file(self.path)
        return iter(store)

    def history(self, instrument: Instrument, schema: MarketDataSchema,
                start: datetime, end: datetime) -> Iterable[Bar]:
        if schema is not MarketDataSchema.BARS:
            raise MarketDataError(
                f"this source provides {SCHEMA} bars only, not {schema.value}")
        previous: Optional[datetime] = None
        for record in self._records():
            if not hasattr(record, "ts_event") or not hasattr(record, "open"):
                continue                      # metadata / symbol-mapping rows
            bar = normalize_ohlcv(record, instrument, self.captured_at,
                                  self.provider)
            if previous is not None and bar.observed_at < previous:
                raise MarketDataError(
                    f"{self.path} goes backwards in time at "
                    f"{bar.observed_at.isoformat()}; every rolling feature "
                    f"would be silently wrong")
            previous = bar.observed_at
            if start <= bar.observed_at < end:
                yield bar
