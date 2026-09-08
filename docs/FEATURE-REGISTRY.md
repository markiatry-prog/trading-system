# Feature and event registry

Generated from the enums in `trading_system/features/records.py`. Every
type listed here is deterministic, timestamped, versioned and
provenance-linked, and none of them expresses a judgement.

**FEATURE** — a state or measurement with a value at every observation.
**EVENT** — something that happened at a moment.

The distinction matters for research: a feature is what you condition
on, an event is what you measure outcomes from.

## The two timestamps

| column | meaning |
|---|---|
| `effective_at` | when the fact became true in market time |
| `available_at` | earliest instant it could have been **known** |

They are equal for most types. They differ for anything needing later
observations to confirm — pivots and fair value gaps. **Research must
filter on `available_at`.** Filtering on `effective_at` introduces
lookahead and yields results that look excellent rather than broken,
which is why `records.as_of()` exists as the only sanctioned filter.

Types with a non-zero confirmation lag are marked **delayed** below.

## Features

| type | delayed | notes |
|---|---|---|
| `session_phase` |  | rth / overnight / closed |
| `vwap` |  | RTH-only, volume-weighted typical price |
| `session_high` |  |  |
| `session_low` |  |  |
| `prior_day_high` |  | **prior-session** — previous completed RTH session |
| `prior_day_low` |  | **prior-session** — previous completed RTH session |
| `overnight_high` |  | **prior-session** — previous overnight window |
| `overnight_low` |  | **prior-session** — previous overnight window |
| `opening_range_high` |  | available only once the range is established |
| `opening_range_low` |  | available only once the range is established |
| `atr` |  | configurable period; None until the window fills |
| `realized_volatility` |  | stdev of bar returns over the configured window |
| `bar_range` |  |  |
| `volume_ratio` |  | bar volume / rolling average |
| `distance_to_vwap` |  |  |
| `distance_to_opening_range_high` |  |  |
| `distance_to_opening_range_low` |  |  |
| `distance_to_prior_day_high` |  | **prior-session** |
| `distance_to_prior_day_low` |  | **prior-session** |
| `distance_to_overnight_high` |  | **prior-session** |
| `distance_to_overnight_low` |  | **prior-session** |
| `above_vwap` |  | state: above / below |
| `swing_high` | **delayed** | pivot; confirmed k bars later |
| `swing_low` | **delayed** | pivot; confirmed k bars later |
| `minutes_since_rth_open` |  | negative before the open; time-of-day studies |

### Prior-session primitives, and why they are marked

The primitives marked **prior-session** are the only ones that read
across a session boundary. Everything else is computed from the current
session's tape alone.

That distinction is not cosmetic. On a continuous series such as
`NQ.c.0` the underlying contract changes at each quarterly roll, so a
level carried into the next session may belong to a contract trading at
a different price, and the gap between them is carry rather than
anything the market did. `trading_system.features.contracts` therefore
refuses to certify a prior-session comparison unless both sessions
resolve to the same contract in the vendor's symbology, and
`trading_system.research.eligibility` scopes that refusal to exactly the
hypotheses that read one of these primitives -- the session itself stays
in the sample for everything else.

The two lists in `features/contracts.py` are derived from this table and
guarded by a test against `FeatureEngine._roll_session`. A primitive
added here that survives a session roll must be added there too, or the
test fails.

## Events

| type | delayed | notes |
|---|---|---|
| `opening_range_established` |  | carries high, low and configured duration |
| `opening_range_high_broken` |  | fires once per session |
| `opening_range_low_broken` |  | fires once per session |
| `structure_break_up` |  | close beyond the last confirmed swing high |
| `structure_break_down` |  | close beyond the last confirmed swing low |
| `liquidity_sweep_high` |  | **prior-session** — wick through the level AND close back inside |
| `liquidity_sweep_low` |  | **prior-session** — wick through the level AND close back inside |
| `displacement_up` |  | true range >= configured ATR multiple |
| `displacement_down` |  | true range >= configured ATR multiple |
| `fvg_formed_up` | **delayed** | three-bar imbalance; effective at the middle bar |
| `fvg_formed_down` | **delayed** | three-bar imbalance; effective at the middle bar |
| `fvg_filled` |  | price traded back into the gap |
| `fvg_inverted` |  | price closed entirely through a filled gap |
| `session_open` |  |  |
| `session_close` |  |  |

## Configuration parameters

Every trading parameter. None is hardcoded in the computation.

| parameter | default | why this default |
|---|---|---|
| `name` | `baseline` | label for the config, not a parameter |
| `session` | see SessionSpec | RTH 09:30-16:00 New York; the cash session ORB/VWAP practice keys off |
| `opening_range_minutes` | `15` | 15 is the most common convention in published ORB work |
| `swing_lookback_bars` | `2` | 2 is the standard fractal; larger k costs confirmation latency |
| `displacement_atr_multiple` | `1.5` | 1.5 is deliberately loose — too tight and it never fires |
| `atr_period_bars` | `14` | 14, conventional |
| `fvg_min_ticks` | `4` | 4 ticks = 1 NQ point; 1 tick would admit constant noise |
| `sweep_min_ticks` | `1` | 1 tick beyond the level, plus a required close back inside |
| `realized_vol_window_bars` | `30` | 30 bars |
| `volume_average_window_bars` | `30` | 30 bars |

Default config digest: `9a9e4683935cc4a868986e5bb35bfa5e266915edef219a5c393c34b50525ec7b`

**These defaults are not claims.** Nothing here asserts a 15-minute
opening range beats a 30-minute one — that is what T-004 exists to
test. They are conventional so results are comparable with published
work, and every one is overridable.

