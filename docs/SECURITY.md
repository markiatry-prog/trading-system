# Security

## 1. The execution air gap — non-negotiable

This system must never possess, retrieve, reference, invoke, or gain
access to credentials, secret-store entries, API scopes, browser
sessions, SDK methods, functions, endpoints, or modules capable of
placing, modifying, cancelling, or otherwise executing trades or orders.

`scripts/check_execution_boundary.py` enforces it, blocking, on every PR
and push, scanning the **entire** repository.

It matches execution **capability signatures**, not vendor names: order
verbs, brokerage/exchange SDK imports, browser-automation imports,
credential names combining a broker/order/execution term with a
secret-like suffix, and order endpoint paths. That distinction is
load-bearing — a read-only market-data integration that legitimately
names a brokerage must not trip it, or the ingestion ticket becomes
unwritable, while a genuine order call must.

The guard carries its own fixture self-test (13 true-positive, 11
true-negative cases) which runs *before* the scan in CI. A guard that no
longer catches what it claims to catch is worse than no guard.

**Market data from a brokerage is permitted.** Order execution is not, at
any point, under any ticket.

## 2. Isolation from the retired Command Center

Separate repository, separate Supabase project, separate deployment,
separate secrets. No submodule, no package dependency, no cross-import,
no shared credential, no connection.

`scripts/check_no_legacy_dependency.py` blocks on: the old project ref,
imports of the old package, its role names, its organizational table
names, its environment variable names, its database host, and any
unprefixed secret name.

Documentation may discuss the retired system freely — that is how we
explain our own decisions. Runtime code and configuration may not
reference it at all. That split is the guard's whole point, and it is
verified in the guard's self-test.

## 3. Database security

### Schema placement
Application tables live in `trading`, never `public`. PostgREST exposes
`public`; `supabase/config.toml` deliberately does not list `trading`, so
the HTTP API surface for application data is empty. Adding a schema there
is a security decision needing its own review.

### anon / authenticated
Revoked to **zero** — table grants, column grants, and default
privileges, in both `trading` and `public`.

This is the posture the retired system did not have. There, both roles
held blanket privileges on every table and were restrained only by RLS
predicates that happened never to match them — one `disable row level
security` away from full exposure. Here there is nothing to fall back on
because there is nothing granted.

### RLS
`ENABLE` **and** `FORCE` on every table. `ENABLE` alone leaves the table
owner exempt; `FORCE` removes that exemption. The retired system used
`ENABLE` only and depended on "the owner never connects at runtime"
remaining true. This system carries money-adjacent data and does not
depend on that.

### Capability identities

Two-tier: a NOLOGIN `_svc` group role holds every grant; a NOLOGIN
`_proc` role inherits it. A LOGIN credential is provisioned per-process
immediately before that process deploys — never in a migration. Rotating
a credential then touches no grant and no policy.

| Identity | Login | Reads | Writes | Forbidden |
|---|---|---|---|---|
| `md_ingest_svc` | no (LOGIN at deploy) | kill switch, provenance spine | `market_observation` artifacts, own runs, component versions | any other stage; kill switch; model versions; lineage edges |
| `feature_engine_svc` | no | kill switch, spine | `feature` + `market_state` artifacts, edges, runs | any other stage; kill switch; model versions |
| `setup_detector_svc` | no | kill switch, spine | `research`, `setup_candidate`, `entry_candidate` artifacts, edges, runs | any other stage; kill switch; model versions |
| `analyst_svc` | no | kill switch, spine | `ai_analysis` artifacts, model versions, edges, runs | **`adversarial_review`**; kill switch |
| `reviewer_svc` | no | kill switch, spine | `adversarial_review` artifacts, model versions, edges, runs | **`ai_analysis`**; kill switch |
| `delivery_svc` | no | kill switch, spine | `delivery` artifacts, edges, runs | any other stage; kill switch |
| `trading_control_svc` | no | kill switch + history | `system_state` (column-scoped), history | **all pipeline work** |

None holds SUPERUSER, CREATEROLE, CREATEDB or BYPASSRLS. None owns a
table. Nobody, including writers, holds UPDATE or DELETE anywhere in the
provenance spine — it is append-only, or it is not provenance.

### The analyst / reviewer separation

The adversarial review is the last check before something reaches a
human, so it must not be authorable by the thing it reviews. That is
enforced by the `stage_scoped_artifact_write` RLS policy, not by
convention: `analyst_svc` inserting an `adversarial_review` fails with
`SQLSTATE 42501`, and `reviewer_svc` inserting an `ai_analysis` fails the
same way. Both directions are asserted in the pgTAP suite.

They also hold separate DSN environment variables, so the separation
survives at the credential layer.

## 4. The kill switch

Fail-closed, unconditionally. If the runtime cannot **positively**
establish that operation is permitted, it behaves as paused. Connection
error, missing row, unrecognized value, timeout, permission error, empty
result — every one is `paused`.

Two states only. A third would require callers to interpret, and an
interpreted kill switch is not one.

**No application component can turn itself back on.** Only
`trading_control_svc` may write `system_state`, it runs no pipeline work,
and `trading_system/system_state.py` exposes no write path at all — which
is asserted by a test that scans the module's own exports.

## 5. Secrets

Every name is `TRADING_`-prefixed, every value is distinct from the
retired system's, and none is in Git. One DSN per capability; there is
deliberately no single "application" credential, because one would
collapse the boundaries above.

T-001 provisions only what T-001 uses: the seven capability DSNs and the
migration DSN. Names for later integrations are recorded in `.env.example`
so the convention is settled, but the credentials are not created until
the ticket that needs them.

No shared Telegram bot. No shared model API credential.

## 6. On capability registries — a deliberate omission

There is none, and that is the design decision, not an oversight.

The retired system had a declarative capability registry. It drifted: its
trading declaration still claimed the tables did not exist long after they
shipped, and its own drift detector could not see scope-content
divergence, so it reported clean. A declaration that can silently diverge
from effective authority is worse than none, because it manufactures
confidence.

Actual grants **are** the authority here, and CI asserts them directly
against the matrix above. If a registry is ever introduced, it must be
mechanically checked against effective grants including scope content —
otherwise it must not exist.
