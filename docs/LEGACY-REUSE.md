# Legacy reuse register

What was taken from the retired Command Center, what was deliberately
left behind, and why. Source of truth for the reuse decision was that
project's own `docs/REUSABLE-ASSET-MANIFEST.md`, written at its freeze.

**Everything reused was COPIED.** There is no submodule, no package
dependency, no cross-import, and no shared credential or database. The
two systems are free to diverge permanently, which is the point.

## Copied as code

| Asset | Source | Adaptation |
|---|---|---|
| `scripts/check_execution_boundary.py` | `trading-intelligence/scripts/check_execution_boundary.py` | Copied, then expanded: default scan target widened from one subdirectory to the whole repository; five pattern families added (directional order actions, position flattening, compound order types, further brokerage/exchange SDK imports, trading-session credential names); five true-positive and five true-negative fixtures added. Self-test grew from 8/6 to 13/11 cases. |

That is the complete list of copied code. Everything else below is a
pattern that was re-implemented for this system's own shape.

## Reused as patterns

| Pattern | Where it came from | How it appears here |
|---|---|---|
| Two-tier NOLOGIN/LOGIN roles | `20260830000007_agent_authorization.sql` | `<name>_svc` group holds grants, `<name>_proc` inherits, LOGIN provisioned at deploy |
| Column-scoped grants excluding identity/timestamps | `20260831000001_trading_intelligence.sql` | e.g. control role may update `state`/`changed_by`/`reason` but not `singleton` |
| Fail-closed kill switch | `chief_of_staff/system_state.py` | `trading_system/system_state.py`, rebuilt; two states; never-raises `check`; no write path |
| Cooperative stop at safe boundaries | V4.0 kill-switch repair | `is_permitted()` for mid-operation re-checks |
| `observed_at` vs `captured_at` | `raw_market_data` | Kept, plus `generated_at` — see `PROVENANCE.md` |
| Evidence vs interpretation separation | Ticket 3 data model | Generalized into a stage-tagged artifact spine |
| Guard self-tests run before the guard | `trading-intelligence-guard.yml` | All three guards, self-test first in CI |
| Ephemeral CI database, no production secrets | `database-tests.yml` | The `database` job |
| Safe error logging (class name, never the message) | `preflight_check.py`, `language_model.py` | `health.report()` returns `type(exc).__name__` only |
| Provider-neutral model gateway | `chief_of_staff/language_model.py` | **Not implemented.** Recorded for the AI-analyst ticket, which is where it earns its place |

## Deliberately NOT carried over

| Left behind | Why |
|---|---|
| `trade_interpretations` / `agent_runs` / `tasks` schema | NOT NULL FK chain meant no interpretation could be recorded unless the orchestrator created a task first. `trading.runs` is self-contained |
| Departments, tasks, objectives, attention/approval queues | Organizational operating-system concepts. This is a trading system |
| Capability registry | Drifted from reality and its own detector could not see it. See `SECURITY.md` §6 |
| Blanket `anon`/`authenticated` grants; `ENABLE`-only RLS | Revoked to zero; RLS is `ENABLE` + `FORCE` |
| pgTAP in the production schema | CI-only here |
| Windows Task Scheduler / desktop-process scheduling | Tied the system to a logged-in machine |
| Unprefixed secret names | Every name is `TRADING_`-prefixed |
| Market-data adapters (Unusual Whales, FRED, CBOE, TradingView) | **Intentionally not copied yet.** They belong to T-002, and T-002 should re-evaluate them against current alternatives rather than inherit them by default |
| The VIX adapter's embedded regime call | It computed a market-state judgment inside a data adapter. That belongs in the market-state engine |
| TradingView flat-file cache and `!=` secret comparison | Reference-only; both need rebuilding (table-backed, HMAC) |

## Additions the retired system lacked

Two CI checks exist here specifically because their absence caused
defects there:

- **migration-ledger parity** — that project drifted four migrations
  without noticing;
- **grants-as-authority verification** — its declarative registry
  diverged from effective grants and reported clean.
