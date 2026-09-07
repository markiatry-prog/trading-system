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

**One documented limit.** `FORCE` removes the *owner* exemption, not the
`BYPASSRLS` attribute. Supabase's `postgres` role carries
`rolbypassrls = true` -- checked on the live project, not assumed -- so
RLS does not constrain `postgres` on this platform no matter what is
FORCEd. Two consequences, both deliberate:

  - The boundary this system rests on is that **no application identity
    is ever `postgres`**. Every runtime process authenticates as a
    `_proc` role, and no `_proc` or `_svc` role holds `BYPASSRLS`
    (asserted, and it is one of the migration-level assertions in the
    database suite).
  - Append-only claims about `system_state_history` and the provenance
    spine are therefore scoped to the capability roles. The database
    suite asserts exactly that, plus the fact that no `UPDATE` or
    `DELETE` policy exists on the history table at all, so those
    commands match no rows for every RLS-subject identity. It does not
    assert "append-only for everyone", because on this platform that
    would be false for `postgres`.

The migration/deploy path uses `postgres` by design; it is the one
identity that may reshape the schema, and it is not a runtime identity.

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

## 7. Deploy-time credential provisioning

`scripts/provision_login_credentials.py` is the second half of the
two-tier pattern. It attaches a LOGIN credential to each `_proc` role
immediately before the process that uses it deploys.

**T-001 deliberately did not run it.** Creating live credentials with no
deployed consumer expands attack surface for zero benefit, and it would
contradict the pattern's own rule that LOGIN arrives at deploy time. The
mechanism is ready; firing it is a deploy step.

Secret handling, by construction:

- passwords are generated inside the script with
  `secrets.token_urlsafe(32)` — never supplied, never guessable;
- they are passed to `ALTER ROLE` as **bound parameters**, never
  interpolated into SQL text, so they cannot reach a server query log;
- they are never printed, logged, committed, or passed as arguments;
- the assembled DSNs are written to one `0600` file whose path the
  operator chooses. That file is the only place they exist outside the
  database. Load it into the platform's variable store, then delete it.

Only `_proc` roles ever receive a credential. The `_svc` group roles hold
the grants and must remain unable to log in — asserted in the test suite,
along with analyst and reviewer keeping separate roles and separate
variables.

```bash
# Preview without touching anything
python3 scripts/provision_login_credentials.py --dry-run \
    --pooler-host <host> --project-ref <ref>

# Provision, at deploy time only
python3 scripts/provision_login_credentials.py \
    --admin-dsn <postgres-dsn> \
    --pooler-host <pooler-host> \
    --project-ref <project-ref> \
    --out /secure/trading-dsns.env
```

Two details of the emitted DSN are load-bearing, and both were wrong in
the first version of this script:

- **The username is `<role>.<project-ref>`, not `<role>`.** Supabase
  fronts Postgres with Supavisor, which multiplexes many projects behind
  one hostname and so authenticates on the tenant-qualified name. A bare
  role name parses as a perfectly valid DSN and then fails at connect
  time, meaning the mistake surfaces only at the deploy healthcheck --
  after seven live credentials have been minted. The script now refuses
  to run against a pooler host without `--project-ref`.
- **Port 5432 (session mode), not 6543 (transaction mode).** Transaction
  mode does not support session-level features, `SET` among them. Every
  authority boundary here is role-scoped, so a pooling mode that forbids
  `SET ROLE` is a trap for every stage built on this foundation, even
  though the current health read does not happen to need it. `--port`
  overrides this for a genuinely short-lived consumer.
