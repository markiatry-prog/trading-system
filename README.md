# trading-system

A hard-isolated trading intelligence system. Deterministic engines compute
market facts; AI reasons over that computed evidence. There is no
brokerage order-execution capability, and there never will be.

**Status: T-001 foundation.** No market data, no features, no market
state, no setups, no AI, no delivery, no scheduling. Those are later
tickets. What exists is the secure, reproducible base they attach to.

## The pipeline this is being built toward

```
MARKET DATA
  -> DETERMINISTIC FEATURE ENGINE      computed, never inferred
  -> MARKET STATE ENGINE               computed, never inferred
  -> HISTORICAL / CONDITIONED RESEARCH
  -> SETUP DETECTOR                    computed, never inferred
  -> ENTRY MODEL / SIGNAL CANDIDATE
  -> AI TRADE ANALYST                  reasons over the above
  -> INDEPENDENT ADVERSARIAL REVIEW    separate identity, by construction
  -> A+ / B / REJECT
  -> TELEGRAM (delivery only)
```

The line that matters: **AI does not calculate deterministic market
facts.** Opening ranges, VWAP, structure, liquidity levels, displacement,
volatility and regime inputs are computed by code that can be tested,
versioned and replayed. The model's job is judgment over evidence, not
arithmetic.

## What T-001 built

- `trading` schema in its own Supabase project — kill switch, and a
  provenance spine every future stage attaches to
- seven capability identities with least-privilege, column-scoped grants;
  **analyst and reviewer are separate database identities**, so a review
  cannot be authored by the thing it reviews
- RLS `ENABLE` **and** `FORCE` on every table; `anon`/`authenticated`
  revoked to zero
- a fail-closed kill switch no application component can turn back on
- three blocking CI guards: execution boundary, Command Center isolation,
  migration-ledger parity
- a container that serves `/health` and proves the stack deploys

## Documentation

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | the North Star and where T-001 stops |
| [`docs/SECURITY.md`](docs/SECURITY.md) | isolation, roles, RLS, air gap, secrets |
| [`docs/MIGRATIONS.md`](docs/MIGRATIONS.md) | the sanctioned workflow and ledger parity |
| [`docs/PROVENANCE.md`](docs/PROVENANCE.md) | the reproducibility contract |
| [`docs/LEGACY-REUSE.md`](docs/LEGACY-REUSE.md) | what was copied from the retired system, and why |

## Running everything CI runs

```bash
make all          # guards + unit tests + ledger check
make db-test      # pgTAP against an ephemeral local stack
```

## The one rule that is never relaxed

No brokerage order-execution capability. Not an SDK, not a credential,
not an endpoint, not browser automation, not "just for testing".
`scripts/check_execution_boundary.py` blocks it in CI, carries its own
fixture self-test, and scans the entire repository.

Market **data** from a brokerage is permitted and expected. Placing,
modifying or cancelling an order is not, under any ticket.
