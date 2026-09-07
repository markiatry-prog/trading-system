# Migrations

## The invariant

```
repository migration versions  ==  applied remote ledger versions
```

Exactly. Same set, same names, no extras on either side.

## Why this is guarded rather than trusted

The retired Command Center developed silent ledger drift. Four migrations
were applied through the Supabase dashboard and recorded under different
version timestamps than the repository files carried:

| Repository | Remote ledger |
|---|---|
| `20260901000001_conversation_turns.sql` | `20260903011339` |
| `20260901000002_context_bootstrap.sql` | `20260903011407` |
| `20260903000001_context_fact_epistemic_metadata.sql` | `20260903212001` |
| `20260903000002_candidate_review_batches.sql` | `20260903222325` |

The schema was correct. Only the ledger diverged — but `supabase db push`
from a clean checkout would have treated all four as unapplied and failed
on already-existing objects. Nobody noticed for days, because nothing
checked. That project's own Makefile prohibited dashboard-only edits; a
rule with no enforcement is a preference.

## The sanctioned workflow

1. Create the migration file locally:
   ```bash
   supabase migration new <snake_case_name>
   ```
   Filename must be `<14-digit-version>_<snake_case_name>.sql`. The guard
   rejects anything else — a malformed name is how a migration becomes
   unmatchable against the ledger.

2. Write forward-only SQL. Every schema change goes here. If it is not in
   `supabase/migrations/`, it does not exist.

3. Test locally against an ephemeral stack:
   ```bash
   make db-test    # supabase db reset && supabase test db
   ```

4. Open a PR. CI runs the parity guard's repository half.

5. Before deploying, run the real invariant against the target:
   ```bash
   TRADING_MIGRATION_DSN=<dsn> python3 scripts/check_migration_ledger_parity.py
   ```

6. Apply with `supabase db push`. Never through the dashboard.

## Dashboard changes are prohibited

The only exception is emergency recovery, and it is not a silent one. If
it happens:

1. Record what was run, by whom, when, and why.
2. Reconcile the repository to match the ledger **the same day**.
3. Run the parity check with a DSN and confirm it passes.
4. Note the incident in this file.

No emergency recovery has occurred. This section exists so the procedure
is settled before it is needed.

## The two modes of the guard

`scripts/check_migration_ledger_parity.py`:

- **local** (no DSN) — validates filename format, version uniqueness and
  ordering. Runs on every PR, needs no credential.
- **remote** (with DSN) — the actual invariant, against
  `supabase_migrations.schema_migrations`.

Local mode passing is **not** evidence the remote is in sync, and the
tool says so on exit, so a green PR check is never mistaken for a
deployable state.

## Current state

Two migrations, and parity was established at birth rather than assumed:
the migrations were applied first, the ledger read back, and the
repository files named to match exactly.

| Version | Name |
|---|---|
| `20260906215558` | `trading_foundation` |
| `20260906220427` | `roles_and_grants` |
