# Provenance

## The question this must answer

> "Did ORB v4 outperform ORB v3 in this regime?"

Years from now, from the database alone, without relying on memory or
undocumented code state. That decomposes into four questions, and the
contract exists so each is answerable:

| Question | Answered by |
|---|---|
| which implementation produced this? | `component_versions.component` + `.version` |
| under which configuration? | `component_versions.config_digest` |
| from which inputs? | `artifact_edges` |
| describing which moment? | `observed_at` / `captured_at` / `generated_at` |
| which model, if any? | `model_versions` |
| which execution? | `runs` |

## Deliberately minimal

This is five tables and one rule. It is not a provenance framework, and
it stops here on purpose: an over-built provenance layer becomes
something stages route around.

```
component_versions ──┐
                     ├──> runs ──> artifacts ──> artifact_edges
model_versions ──────┘                (parent/child lineage)
```

Future stage tables carry their own domain columns and a foreign key to
`artifacts.id`. That is the extension point. The spine never learns any
stage's shape; no stage reinvents lineage or timestamps.

## The three timestamps

The part most likely to be eroded by a hurried change, so it is stated
plainly:

| Column | Meaning |
|---|---|
| `observed_at` | when the fact was true in the market |
| `captured_at` | when we received it |
| `generated_at` | when this artifact was computed |

A feature computed at 09:31 from a bar that closed at 09:30 and reached
us at 09:30:04 has three different times. Collapsing them makes a stale
input indistinguishable from a live one — precisely the error a
deterministic feature engine must never make.

The retired system separated `observed_at` from `captured_at` and its
reasoning was sound: CBOE publishes end-of-day closes, FRED lags a day
over weekends. `generated_at` is the addition here, because that system
had no computation stage to date.

## The rules, enforced in code

`trading_system/provenance.py` refuses to produce an unreconstructable
record rather than warning about it:

- a `market_observation` **must** carry both `observed_at` and
  `captured_at` — without them downstream staleness checks have nothing
  to work from;
- every **derived** artifact must have at least one parent — a derived
  artifact with no inputs cannot be reconstructed;
- every artifact must carry a `run_id`;
- timestamps must be timezone-aware (never `datetime.utcnow()`, which
  returns a naive value that compares wrongly against `timestamptz`);
- the stage must be one of the nine known stages.

## Why models are versioned separately

An AI stage has **both** a component version (its harness, parsing,
guardrails) and a model version (provider, model, prompt version,
parameters). Collapsing them would make "same prompt, new model"
indistinguishable from "new prompt, same model" — the exact comparison an
adversarial-review pipeline needs to make.

## Config digests

`config_digest` is a SHA-256 over the canonicalized configuration, sorted
keys and fixed separators, so the same logical configuration hashes
identically across processes and Python versions. Without that
stability, "same config" comparisons silently fail and the ORB v3/v4
question becomes unanswerable.

## Append-only

Nobody — including writers — holds UPDATE or DELETE on any spine table.
Provenance you can rewrite is not provenance.
