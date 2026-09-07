"""The canonical market-data contract.

Every provider-specific adapter normalises into the records defined here,
and the deterministic feature engine reads ONLY these records. That is
the whole purpose: the live provider is currently undecided and will
almost certainly change at least once, and a feature engine written
against a vendor's wire format would have to be rewritten each time --
which would also invalidate every backtest built on the old shape.

WHAT THIS MODULE DELIBERATELY DOES NOT CONTAIN

  - No provider SDK import, no network call, no credential read. A source
    is handed to this contract, never constructed by it.
  - No database handle. Sources yield records; the ingestion process
    decides what to persist. A source that could write canonical state
    would put a vendor's parser inside our provenance boundary.
  - No order, position or account concept of any kind. This is a
    read-only intelligence pipeline. `Side` here means the side of a
    quote or the aggressor of a trade -- market structure, not intent.

DEPTH IS NOT ASSUMED

The ticket's strategies -- ORB, VWAP, BOS/structure, liquidity/FVG,
regime -- are all expressible over trades, top-of-book and bars. Book
depth (L2/MBO) is an order of magnitude more expensive to source and
store, so it is not modelled here. `MarketDataSchema` has room for it,
and the day empirical testing shows depth materially improves expectancy
is the day it earns its cost. Not before.

PRICES ARE DECIMAL, NEVER FLOAT

NQ moves in 0.25 tick increments and ES in 0.25; float arithmetic makes
`price % tick_size == 0` intermittently false and turns an exact level
test into a flaky one. Providers that publish scaled integers (Databento
uses 1e-9 fixed point) go through `price_from_fixed`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Iterable, Iterator, Optional, Protocol, Sequence

from .provenance import digest, utcnow

# How far a provider's clock may run ahead of ours before a record is
# rejected as implausible rather than merely skewed. Real feeds routinely
# show tens of milliseconds; a capture time an hour before the event it
# describes is a bug, not skew.
MAX_CLOCK_SKEW = timedelta(minutes=5)


class MarketDataError(Exception):
    """A record that cannot be trusted. Raised at the adapter boundary so
    bad data never reaches the feature engine wearing canonical clothes."""


class Side(str, Enum):
    """Quote side, or the aggressing side of a trade.

    NOT an instruction. This system has no order capability and never
    will; `Side.BID` describes what the market did, not what we intend.
    """
    BID = "bid"
    ASK = "ask"
    NONE = "none"


class MarketDataSchema(str, Enum):
    """What a source can yield. Kept deliberately small."""
    TRADES = "trades"
    TOP_OF_BOOK = "top_of_book"
    BARS = "bars"


@dataclass(frozen=True)
class Instrument:
    """A tradeable contract, identified the way a human names it.

    `symbol` is the specific contract (NQZ6); `product` is the root (NQ),
    which is what strategy configuration refers to so that a rule survives
    the quarterly roll. `tick_size` lives here because level tests are
    meaningless without it.
    """
    symbol: str
    product: str
    venue: str = "GLBX"
    tick_size: Decimal = Decimal("0.25")

    def __post_init__(self) -> None:
        if not self.symbol or not self.product:
            raise MarketDataError("instrument needs both a symbol and a product")
        if self.tick_size <= 0:
            raise MarketDataError(f"tick_size must be positive, got {self.tick_size}")

    def is_on_tick(self, price: Decimal) -> bool:
        return (Decimal(price) % self.tick_size) == 0


def price_from_fixed(value: int, exponent: int = -9) -> Decimal:
    """Scaled-integer price -> Decimal, exactly.

    Databento publishes 1e-9 fixed point. Going through float here would
    reintroduce the very error Decimal exists to avoid, so the scaling is
    done in Decimal throughout.
    """
    return Decimal(int(value)).scaleb(int(exponent))


def _check_times(observed_at: datetime, captured_at: datetime, what: str) -> None:
    for name, ts in (("observed_at", observed_at), ("captured_at", captured_at)):
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise MarketDataError(
                f"{what}.{name} must be timezone-aware -- naive datetimes "
                f"compare wrongly against timestamptz"
            )
    if observed_at - captured_at > MAX_CLOCK_SKEW:
        raise MarketDataError(
            f"{what}.observed_at is more than {MAX_CLOCK_SKEW} ahead of "
            f"captured_at; the feed clock or the parse is wrong"
        )


@dataclass(frozen=True)
class Trade:
    """One execution printed by the exchange.

    `observed_at` is the exchange's event time and `captured_at` is when
    we received it. Both are kept because the gap between them is the
    only honest measure of feed latency, and a backtest that silently
    uses capture time where live uses event time is lying to itself.
    """
    instrument: Instrument
    price: Decimal
    size: int
    observed_at: datetime
    captured_at: datetime
    aggressor: Side = Side.NONE
    sequence: Optional[int] = None
    provider: str = "unknown"

    def __post_init__(self) -> None:
        _check_times(self.observed_at, self.captured_at, "Trade")
        if self.size <= 0:
            raise MarketDataError(f"trade size must be positive, got {self.size}")
        if not isinstance(self.price, Decimal):
            raise MarketDataError(
                f"trade price must be Decimal, got {type(self.price).__name__} "
                f"-- float prices break exact tick and level comparisons"
            )


@dataclass(frozen=True)
class TopOfBook:
    """Best bid and offer at a point in time.

    A crossed or locked book is reported, not rejected. Conflated feeds
    (CME's cloud API conflates top of book at 500 ms) genuinely produce
    these, and a contract that refuses real data would force adapters to
    silently drop it. `is_crossed` lets the feature engine decide.
    """
    instrument: Instrument
    bid_price: Optional[Decimal]
    bid_size: Optional[int]
    ask_price: Optional[Decimal]
    ask_size: Optional[int]
    observed_at: datetime
    captured_at: datetime
    provider: str = "unknown"

    def __post_init__(self) -> None:
        _check_times(self.observed_at, self.captured_at, "TopOfBook")
        for name in ("bid_price", "ask_price"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Decimal):
                raise MarketDataError(f"TopOfBook.{name} must be Decimal or None")

    @property
    def is_crossed(self) -> bool:
        if self.bid_price is None or self.ask_price is None:
            return False
        return self.bid_price > self.ask_price

    @property
    def mid(self) -> Optional[Decimal]:
        if self.bid_price is None or self.ask_price is None:
            return None
        return (self.bid_price + self.ask_price) / 2


@dataclass(frozen=True)
class Bar:
    """An aggregated interval.

    `interval_seconds` rather than an enum of blessed intervals: 1s, 1m
    and 5m are all just numbers, and ORB in particular wants an opening
    range that is not necessarily a standard bar width.
    """
    instrument: Instrument
    interval_seconds: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    observed_at: datetime          # bar OPEN time, exchange clock
    captured_at: datetime
    trade_count: Optional[int] = None
    provider: str = "unknown"

    def __post_init__(self) -> None:
        _check_times(self.observed_at, self.captured_at, "Bar")
        if self.interval_seconds <= 0:
            raise MarketDataError("bar interval must be positive")
        if self.volume < 0:
            raise MarketDataError("bar volume cannot be negative")
        for name in ("open", "high", "low", "close"):
            if not isinstance(getattr(self, name), Decimal):
                raise MarketDataError(f"Bar.{name} must be Decimal")
        if self.low > self.high:
            raise MarketDataError(f"bar low {self.low} exceeds high {self.high}")
        for name in ("open", "close"):
            value = getattr(self, name)
            if not (self.low <= value <= self.high):
                raise MarketDataError(
                    f"bar {name} {value} outside [low {self.low}, high {self.high}]"
                )

    @property
    def closed_at(self) -> datetime:
        return self.observed_at + timedelta(seconds=self.interval_seconds)


Record = object  # Trade | TopOfBook | Bar; union syntax kept off for 3.9


class MarketDataSource(Protocol):
    """What every adapter implements.

    Two methods, both optional in practice: an adapter declares what it
    can do through `schemas`, and callers check rather than assume. A
    historical-only source (the $0 path while the live provider is
    undecided) simply advertises no live capability.
    """

    name: str

    def schemas(self) -> Sequence[MarketDataSchema]:
        """Which record types this source can produce."""
        ...

    def history(
        self,
        instrument: Instrument,
        schema: MarketDataSchema,
        start: datetime,
        end: datetime,
    ) -> Iterable[Record]:
        """Bounded replay of a past window, ordered by observed_at."""
        ...


def records_digest(records: Sequence[Record]) -> str:
    """Content digest over a batch, for the provenance spine.

    Keyed on the canonical fields rather than the provider's payload, so
    the same market window ingested from two different vendors produces
    the same digest -- which is exactly the comparison that tells us
    whether a provider swap changed the data underneath a backtest.
    """
    canonical = []
    for r in records:
        if isinstance(r, Trade):
            canonical.append(["trade", r.instrument.symbol, str(r.price),
                              r.size, r.observed_at.isoformat(), r.aggressor.value])
        elif isinstance(r, TopOfBook):
            canonical.append(["tob", r.instrument.symbol,
                              str(r.bid_price), r.bid_size,
                              str(r.ask_price), r.ask_size,
                              r.observed_at.isoformat()])
        elif isinstance(r, Bar):
            canonical.append(["bar", r.instrument.symbol, r.interval_seconds,
                              str(r.open), str(r.high), str(r.low), str(r.close),
                              r.volume, r.observed_at.isoformat()])
        else:
            raise MarketDataError(f"not a canonical record: {type(r).__name__}")
    return digest(canonical)
