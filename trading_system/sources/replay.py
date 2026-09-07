"""A deterministic file-backed source. No network, no credentials, $0.

This exists so the feature engine can be built and tested before the live
provider question is settled. It is a real adapter implementing the real
contract -- not a mock -- so a strategy validated against replayed data is
running the same code path it will run against a vendor feed.

It is also the reference for what every other adapter must do: parse into
Decimal, carry both timestamps, and refuse anything it cannot vouch for.

CSV columns (header required, order irrelevant):
    trades       observed_at,price,size[,aggressor][,sequence]
    top_of_book  observed_at,bid_price,bid_size,ask_price,ask_size
    bars         observed_at,open,high,low,close,volume[,trade_count]

`observed_at` is ISO 8601 and MUST carry an offset. A bare local
timestamp is the single most common way a futures dataset silently
shifts by hours, so it is rejected rather than guessed at.
"""
from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from ..market_data import (
    Bar,
    Instrument,
    MarketDataError,
    MarketDataSchema,
    Record,
    Side,
    TopOfBook,
    Trade,
)
from ..provenance import utcnow


def _decimal(row: dict, key: str, required: bool = True) -> Optional[Decimal]:
    raw = (row.get(key) or "").strip()
    if not raw:
        if required:
            raise MarketDataError(f"missing required column {key!r}")
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise MarketDataError(f"{key}={raw!r} is not a number") from None


def _int(row: dict, key: str, required: bool = True) -> Optional[int]:
    raw = (row.get(key) or "").strip()
    if not raw:
        if required:
            raise MarketDataError(f"missing required column {key!r}")
        return None
    try:
        return int(raw)
    except ValueError:
        raise MarketDataError(f"{key}={raw!r} is not an integer") from None


def parse_observed_at(raw: str) -> datetime:
    raw = (raw or "").strip()
    if not raw:
        raise MarketDataError("observed_at is empty")
    text = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise MarketDataError(f"observed_at={raw!r} is not ISO 8601") from None
    if parsed.tzinfo is None:
        raise MarketDataError(
            f"observed_at={raw!r} has no UTC offset. A naive futures "
            f"timestamp is ambiguous across the session boundary and is "
            f"the usual cause of a dataset silently shifted by hours."
        )
    return parsed


class CsvReplaySource:
    """Replays canonical CSV. Implements `MarketDataSource`."""

    def __init__(self, path, instrument: Instrument, interval_seconds: int = 60):
        self.path = Path(path)
        self.instrument = instrument
        self.interval_seconds = interval_seconds
        self.name = f"replay:{self.path.name}"

    def schemas(self) -> Sequence[MarketDataSchema]:
        return (
            MarketDataSchema.TRADES,
            MarketDataSchema.TOP_OF_BOOK,
            MarketDataSchema.BARS,
        )

    def history(
        self,
        instrument: Instrument,
        schema: MarketDataSchema,
        start: datetime,
        end: datetime,
    ) -> Iterable[Record]:
        """Yields records with observed_at in [start, end), ordered.

        Ordering is asserted rather than assumed: an out-of-order file
        would make every rolling feature quietly wrong, and that is far
        harder to notice later than an exception here.
        """
        captured_at = utcnow()
        previous: Optional[datetime] = None
        with self.path.open(newline="") as handle:
            for lineno, row in enumerate(csv.DictReader(handle), start=2):
                observed_at = parse_observed_at(row.get("observed_at", ""))
                if previous is not None and observed_at < previous:
                    raise MarketDataError(
                        f"{self.path}:{lineno} goes backwards in time "
                        f"({observed_at.isoformat()} after "
                        f"{previous.isoformat()}); rolling features would "
                        f"be silently wrong"
                    )
                previous = observed_at
                if not (start <= observed_at < end):
                    continue
                try:
                    yield self._record(schema, row, observed_at, captured_at, instrument)
                except MarketDataError as exc:
                    raise MarketDataError(f"{self.path}:{lineno}: {exc}") from None

    def _record(self, schema, row, observed_at, captured_at, instrument) -> Record:
        if schema is MarketDataSchema.TRADES:
            aggressor = (row.get("aggressor") or "none").strip().lower()
            if aggressor not in {s.value for s in Side}:
                raise MarketDataError(f"aggressor={aggressor!r} is not a Side")
            return Trade(
                instrument=instrument,
                price=_decimal(row, "price"),
                size=_int(row, "size"),
                observed_at=observed_at,
                captured_at=captured_at,
                aggressor=Side(aggressor),
                sequence=_int(row, "sequence", required=False),
                provider=self.name,
            )
        if schema is MarketDataSchema.TOP_OF_BOOK:
            return TopOfBook(
                instrument=instrument,
                bid_price=_decimal(row, "bid_price", required=False),
                bid_size=_int(row, "bid_size", required=False),
                ask_price=_decimal(row, "ask_price", required=False),
                ask_size=_int(row, "ask_size", required=False),
                observed_at=observed_at,
                captured_at=captured_at,
                provider=self.name,
            )
        if schema is MarketDataSchema.BARS:
            return Bar(
                instrument=instrument,
                interval_seconds=self.interval_seconds,
                open=_decimal(row, "open"),
                high=_decimal(row, "high"),
                low=_decimal(row, "low"),
                close=_decimal(row, "close"),
                volume=_int(row, "volume"),
                observed_at=observed_at,
                captured_at=captured_at,
                trade_count=_int(row, "trade_count", required=False),
                provider=self.name,
            )
        raise MarketDataError(f"unsupported schema {schema!r}")
