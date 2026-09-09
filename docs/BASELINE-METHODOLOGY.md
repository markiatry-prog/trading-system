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

## The inference unit is the SESSION, not the event

Events inside one session share a volatility regime, a news cycle, and
frequently overlapping forward windows — two opening-range breaks twenty
minutes apart are largely the same forty minutes of tape. Treating them as
independent draws is not merely optimistic; it is the mechanism by which
one unusual week becomes a discovery.

- **Interval**: percentile bootstrap resampling **sessions with
  replacement**, retaining every eligible observation belonging to a drawn
  session, in both arms and across every stratum. A session drawn twice
  contributes twice. Both arms are drawn from the *same* sampled sessions,
  because a session's events and its controls share whatever made that
  session unusual.
- **Matching strata are preserved inside each resample** — the lift is
  recomputed by the same standardisation, and a resample that happens to
  omit a stratum renormalises over the rest rather than comparing unlike
  things.
- **p-value**: two-sided, from that same clustered distribution. No
  distributional assumption — one-minute forward returns are heavy-tailed
  and skewed.
- **Below 10 supporting sessions**, no interval is reported at all. A wide
  interval is honest; a fabricated one is not.

### How repeated events from one session contribute

A session's events all enter the point estimate — none are discarded, and
a session with forty events contributes all forty to the stratified mean.
What changes is the *uncertainty*: because the resampling unit is the
session, those forty rise and fall together across bootstrap draws, so they
widen the interval instead of narrowing it.

**Effective cluster count** (Kish, `(Σnₛ)² / Σnₛ²`) is reported next to the
raw event count. It equals the session count when events are spread evenly
and collapses toward 1 when one session dominates. A large gap between the
two is the warning that a raw event count overstates the evidence.

Measured on synthetic data where almost every event comes from three
sessions:

| | events | effective clusters | 95% CI | width | p |
|---|---|---|---|---|---|
| event-level (IID) | 147 | — | [+287.5, +326.8] | 39.3 | 0.003 |
| **session-clustered** | 147 | **4.5** | [+0.00, +316.3] | **316.3** | **0.053** |

The event-level interval is 8× too narrow and reports a rock-solid finding.
When events *are* spread evenly across 30 sessions, the clustered interval
is 0.9× the event-level one — so the clustering is calibrated, not merely
conservative.

The event-level interval and permutation p-value are still reported, purely
so the size of that difference is visible per hypothesis, together with a
`clustering_changes_the_conclusion` flag. **They are never the result.**

Reported per hypothesis: event and control arm statistics with both sample
sizes, unique sessions, effective clusters, the standardised comparator,
absolute effect, lift, clustered lift CI and p-value, the event-level
comparison, MFE/MAE lift, and the ordering ("+X before −Y") lift.

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
