"""The FROZEN T-004 hypothesis set.

Registered here, in version control, BEFORE any real data existed. That
is the whole point: these were written against synthetic fixtures, and
the commit history proves they were not chosen after seeing outcomes.

They must not be edited in response to results. Adding, removing or
altering a hypothesis after seeing real data invalidates every
multiple-comparison correction and turns the study into a search.

Each states a direction and an explicit falsifier. None presumes an
edge. Several are expected to be REJECTED, and that is a successful
outcome -- it removes a candidate before anything is built on it.
"""
from __future__ import annotations

from .hypotheses import Direction, HypothesisRegistry


def build_registry() -> HypothesisRegistry:
    r = HypothesisRegistry()

    r.register(
        "H1", "An opening-range high break is followed by continuation up",
        "opening_range_high_broken", Direction.UP, 30, {},
        "mean 30-minute forward return CI includes zero, or is negative")

    r.register(
        "H2", "An ORB high break occurring above VWAP continues more reliably",
        "opening_range_high_broken", Direction.UP, 30, {"above_vwap": "above"},
        "CI includes zero, or does not differ from the unconditional case")

    r.register(
        "H3", "An opening-range low break is followed by continuation down",
        "opening_range_low_broken", Direction.DOWN, 30, {},
        "mean signed 30-minute forward return CI includes zero")

    r.register(
        "H4", "Upward displacement is followed by further upside",
        "displacement_up", Direction.UP, 15, {},
        "mean 15-minute forward return CI includes zero")

    r.register(
        "H5", "Downward displacement is followed by further downside",
        "displacement_down", Direction.DOWN, 15, {},
        "mean signed 15-minute forward return CI includes zero")

    r.register(
        "H6", "A prior-day-high sweep is followed by reversal down",
        "liquidity_sweep_high", Direction.DOWN, 60, {},
        "mean signed 60-minute forward return CI includes zero or is positive")

    r.register(
        "H7", "A prior-day-low sweep is followed by reversal up",
        "liquidity_sweep_low", Direction.UP, 60, {},
        "mean 60-minute forward return CI includes zero or is negative")

    r.register(
        "H8", "An upward structure break is followed by continuation up",
        "structure_break_up", Direction.UP, 30, {},
        "mean 30-minute forward return CI includes zero")

    r.register(
        "H9", "A downward structure break is followed by continuation down",
        "structure_break_down", Direction.DOWN, 30, {},
        "mean signed 30-minute forward return CI includes zero")

    r.register(
        "H10", "An upward fair-value gap is followed by continuation up",
        "fvg_formed_up", Direction.UP, 30, {},
        "mean 30-minute forward return CI includes zero")

    r.register(
        "H11", "A downward fair-value gap is followed by continuation down",
        "fvg_formed_down", Direction.DOWN, 30, {},
        "mean signed 30-minute forward return CI includes zero")

    r.register(
        "H12", "An FVG inversion is followed by movement in the new direction",
        "fvg_inverted", Direction.DOWN, 30, {},
        "mean signed 30-minute forward return CI includes zero")

    r.seal()
    return r
