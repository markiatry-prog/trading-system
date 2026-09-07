# Architecture

## North Star

```
MARKET DATA
  -> DETERMINISTIC FEATURE ENGINE
  -> MARKET STATE ENGINE
  -> HISTORICAL / CONDITIONED RESEARCH
  -> SETUP DETECTOR
  -> ENTRY MODEL / SIGNAL CANDIDATE
  -> AI TRADE ANALYST
  -> INDEPENDENT ADVERSARIAL REVIEW
  -> A+ / B / REJECT
  -> TELEGRAM / USER ATTENTION
```

### The division of labour

**Deterministic layers calculate facts.** Opening-range highs and lows,
VWAP, price structure, liquidity levels, displacement measurements,
volatility measurements, regime inputs, and setup conditions are computed
by code. They are testable, versionable, and replayable against history.

**AI reasons over computed evidence.** It does not derive the numbers it
argues about. This is not a stylistic preference: a model that computes
its own inputs cannot be backtested, cannot be compared across versions,
and cannot be held to an evidence trail.

**Telegram is a delivery surface**, not the intelligence engine.

**No live order execution exists.** See `SECURITY.md`.

## Where T-001 stops

T-001 built the foundation and none of the pipeline. Concretely, the
following do **not** exist in this repository and must not be added
outside their own tickets: market-data ingestion of any kind, ORB, VWAP,
BOS, FVG/IFVG, liquidity detection, market-state classification,
historical analog search, setup detection, entry models, signal
generation, AI analysis, adversarial review logic, Telegram messaging,
scheduling, paper trading, live trading.

What exists: the schema, the identities, the kill switch, the provenance
spine, the guards, and a health service.

## The shape future stages take

Every stage follows the same pattern, which is why the spine is worth
having before any stage exists:

1. Register (or look up) its `component_version` — implementation plus
   configuration digest.
2. Open a `run`.
3. Read the kill switch; stop if not permitted.
4. Do its work.
5. Write its domain rows, each attached to an `artifact` row carrying
   `observed_at` / `captured_at` / `generated_at`.
6. Record `artifact_edges` from the inputs it consumed.
7. Close the run with an outcome.

A stage's domain table is its own; it holds a foreign key to
`trading.artifacts`. The spine never learns any stage's shape, and no
stage reinvents lineage or timestamps.

## What this system deliberately is not

It is not an organizational operating system. There are no tasks,
objectives, initiatives, departments, attention queues, approval queues,
or capability registries. The retired Command Center had all of those,
and its trading layer was chained to them by NOT NULL foreign keys such
that no interpretation could be recorded unless the orchestrator created
a task first. That coupling is the specific thing this design exists to
avoid: a `run` here is self-contained.
