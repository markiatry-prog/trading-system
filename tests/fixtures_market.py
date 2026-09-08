"""Deterministic synthetic bars. No randomness anywhere: a fixture that
differs between runs cannot prove determinism."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading_system.market_data import Bar, Instrument

NQ = Instrument(symbol="NQZ6", product="NQ", tick_size=Decimal("0.25"))
ES = Instrument(symbol="ESZ6", product="ES", tick_size=Decimal("0.25"))

# 2026-01-12 is a Monday and not a holiday. RTH opens 09:30 New York,
# which is 14:30 UTC in January.
RTH_OPEN = datetime(2026, 1, 12, 14, 30, tzinfo=timezone.utc)


def bar(minute_offset, o, h, l, c, volume=100, instrument=NQ,
        origin=RTH_OPEN, interval=60, contract_id=None):
    t = origin + timedelta(minutes=minute_offset)
    return Bar(
        instrument=instrument,
        interval_seconds=interval,
        open=Decimal(str(o)), high=Decimal(str(h)),
        low=Decimal(str(l)), close=Decimal(str(c)),
        volume=volume,
        observed_at=t,
        captured_at=t + timedelta(milliseconds=50),
        provider="fixture",
        contract_id=contract_id,
    )


def flat_series(n, start=20000, instrument=NQ, origin=RTH_OPEN):
    """n identical-shaped bars stepping up one point each minute."""
    out = []
    for i in range(n):
        base = Decimal(start) + i
        out.append(bar(i, base, base + 2, base - 2, base + 1,
                       instrument=instrument, origin=origin))
    return out


def series_with_swing_high(peak_at=5, n=12, start=20000):
    """A clean single peak so pivot confirmation is unambiguous."""
    out = []
    for i in range(n):
        if i == peak_at:
            o, h, l, c = start + 10, start + 20, start + 8, start + 12
        else:
            dist = abs(i - peak_at)
            top = start + 10 - dist * 3
            o, h, l, c = top - 2, top, top - 4, top - 1
        out.append(bar(i, o, h, l, c))
    return out
