"""Environment access, with one job beyond os.environ: strip the damage a
copy-paste through a shell, a web form, or a secrets UI does to a value.

Every secret this system reads is TRADING_-prefixed. That is not cosmetic:
the retired system used unprefixed messaging and model-provider credential
names, and a single shared environment variable name is how two systems
silently end up on one bot and one billing account. The prefix makes
accidental sharing require a deliberate rename, which someone reviews.
"""
from __future__ import annotations

import os
from typing import Optional

# Wrapping quotes and stray whitespace are the two failure modes actually
# observed in practice when a DSN or token is pasted into a hosting
# provider's variable UI. Both produce authentication errors that look
# like a wrong credential rather than a malformed one.
_WRAPPING_QUOTES = ("'", '"')


def clean(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in _WRAPPING_QUOTES:
        v = v[1:-1].strip()
    return v


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return clean(raw)


def require(name: str) -> str:
    value = get(name)
    if value is None:
        raise MissingSetting(name)
    return value


class MissingSetting(Exception):
    """Raised for a setting with no safe default. The message names the
    variable and never its value."""

    def __init__(self, name: str):
        super().__init__(f"{name} is not set")
        self.name = name
