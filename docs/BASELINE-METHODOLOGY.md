# Baseline-relative measurement

Baseline version **1.0.0** · module `trading_system/research/baseline.py`

## The question that changed

T-004 asked whether the post-event return differs from **zero**. Over
2021–2026 NQ trended upward, and its drift is far from uniform through the
day: the hour after the RTH open behaves nothing like the middle of the
overnight session. So an event that merely *clusters* at an active time
inherits that time's drift and reads as an edge. With samples in the
thousands, a fraction of a point per horizon is enough to push a
confidence interval off zero.

T-004B asks the question that survives contact with a trending market:

> Does this event change the forward distribution relative to a comparable
> **non-event** state?

## How the control is built

For each hypothesis, a control arm is drawn from moments that resemble the
event's moments in everything except the event.

| matched on | why |
|---|---|
| instrument | a different contract has a different tick and price level |
| time of day | intraday seasonality is large, and events cluster in it |
| session eligibility | same quality gate and contract-provenance constraints as the event arm |
| volatility regime | ATR knowable *at* the anchor over the median closing ATR of the previous 20 **completed** sessions |
| forward horizon | identical by construction |

**Not** matched: anything that is part of the signal. Matching an
opening-range-break control on "price is above the opening range high"
would select controls that are themselves breakouts, driving the lift to
zero by construction — a way to prove nothing while looking rigorous.

**No lookahead.** The volatility reference is fixed before the current
session opens; the ATR at an anchor comes through the same `available_at`
filter the study uses everywhere. Every stratum could have been computed
live, at that anchor.

## The estimator

The control mean is a **direct-standardised** average: per-stratum control
means, weighted by how the *events* are distributed across strata. It is
deterministic — no matched-pair draw, so no sampling noise in the point
estimate.

- **Interval**: percentile bootstrap over *both* arms. Resampling only the
  events would treat the control mean as exactly known and report an
  interval narrower than the evidence supports.
- **p-value**: stratified permutation, relabelling event and control within
  each stratum. Neither assumes a distribution — one-minute forward returns
  are heavy-tailed and skewed.

Reported per hypothesis: event and control arm statistics with both sample
sizes, the standardised comparator, absolute effect, lift, lift CI, lift
p-value, MFE/MAE lift, and the ordering ("+X before −Y") lift.

## Two known biases, both conservative

1. **Control contamination.** A control anchor within one horizon *before*
   an event captures that event's move too. This pushes lift toward zero.
   Moments near an event are deliberately left in the pool: removing them
   would redefine the control as "the market when nothing was happening",
   a different and much easier comparison.
2. **Thin strata.** Events whose stratum has fewer than the declared
   minimum of controls are dropped from the lift and **counted** in
   `events_dropped_for_thin_strata`, rather than compared against a
   handful of observations.

## Status of T-004B

Diagnostic and **exploratory**, not confirmatory. The methodology was
revised after the original NQ results were seen, so it is a second look at
the same data. It does not supersede the T-004 report, which remains the
record of what was actually preregistered and measured. An interesting lift
here is a **candidate to preregister** and test on data it has not seen.

The final holdout is not read. The T-004B runner has no unseal flag at all.

## T-005 dependency (binding)

T-005 Machine Discovery must use **baseline-relative lift** as its primary
discovery target. The search objective is

```
conditional future distribution  −  matched baseline future distribution
```

not raw future returns. A discovery engine optimising raw forward returns
over a trending series will rediscover secular drift, time-of-day drift and
regime effects, rank them as edges, and present them with the confidence
that large samples produce. The baseline is what prevents that, and it must
be inside the objective rather than applied as a filter afterwards — a
filter applied to the top of a drift-ranked list only removes the ones that
were already obvious.
