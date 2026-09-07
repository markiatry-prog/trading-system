-- Database security assertions, run against an EPHEMERAL CI stack.
-- pgTAP is never installed in the production application schema.
--
-- These test BEHAVIOUR (can this role actually do this?) rather than
-- configuration text wherever a stronger check is practical.

begin;
select plan(32);

-- ---------------------------------------------------------------------
-- Schema placement and API surface
-- ---------------------------------------------------------------------
select has_schema('trading', 'the trading schema exists');
select is(
  (select count(*)::int from pg_class c join pg_namespace n on n.oid=c.relnamespace
   where n.nspname='public' and c.relkind='r'),
  0, 'no application tables live in public (PostgREST exposes public)'
);

-- ---------------------------------------------------------------------
-- anon / authenticated hold nothing. This is the posture the retired
-- system did NOT have: there, both held blanket privileges on every
-- table and were restrained only by RLS predicates that happened not to
-- match them.
-- ---------------------------------------------------------------------
select is(
  (select count(*)::int from information_schema.role_table_grants
   where grantee in ('anon','authenticated') and table_schema in ('trading','public')),
  0, 'anon and authenticated have zero application-table grants'
);
select is(
  (select count(*)::int from information_schema.role_column_grants
   where grantee in ('anon','authenticated') and table_schema='trading'),
  0, 'anon and authenticated have zero column-level grants'
);

-- ---------------------------------------------------------------------
-- RLS: ENABLE *and* FORCE. FORCE is the deliberate upgrade -- ENABLE
-- alone leaves the table owner exempt.
-- ---------------------------------------------------------------------
select is(
  (select count(*)::int from pg_class c join pg_namespace n on n.oid=c.relnamespace
   where n.nspname='trading' and c.relkind='r' and not (c.relrowsecurity and c.relforcerowsecurity)),
  0, 'every trading table has RLS both ENABLED and FORCED'
);

-- ---------------------------------------------------------------------
-- Runtime identities carry no elevated attribute and cannot log in
-- (LOGIN is provisioned per-process at deploy, never in a migration).
-- ---------------------------------------------------------------------
select is(
  (select count(*)::int from pg_roles
   where (rolname like '%\_svc' or rolname like '%\_proc')
     and (rolsuper or rolcreaterole or rolcreatedb or rolbypassrls)),
  0, 'no capability role has SUPERUSER/CREATEROLE/CREATEDB/BYPASSRLS'
);
select is(
  (select count(*)::int from pg_roles
   where (rolname like '%\_svc' or rolname like '%\_proc') and rolcanlogin),
  0, 'no capability role has LOGIN in the migration-defined state'
);
select is(
  (select count(*)::int from pg_class c join pg_namespace n on n.oid=c.relnamespace
   join pg_roles r on r.oid = c.relowner
   where n.nspname='trading' and (r.rolname like '%\_svc' or r.rolname like '%\_proc')),
  0, 'no application role owns any table'
);

-- ---------------------------------------------------------------------
-- The kill switch: exactly one writer, and it is not a pipeline stage.
-- ---------------------------------------------------------------------
select is(
  (select count(*)::int from information_schema.role_column_grants
   where table_schema='trading' and table_name='system_state'
     and privilege_type in ('UPDATE','INSERT')
     and grantee like '%\_svc' and grantee <> 'trading_control_svc'),
  0, 'no capability role other than trading_control_svc may write system_state'
);
select ok(
  (select count(*) > 0 from information_schema.role_column_grants
   where table_schema='trading' and table_name='system_state'
     and privilege_type='UPDATE' and grantee='trading_control_svc'),
  'trading_control_svc holds the UPDATE grant on system_state'
);
select is(
  (select count(*)::int from information_schema.role_column_grants
   where table_schema='trading' and table_name='system_state'
     and grantee='trading_control_svc' and column_name='singleton'
     and privilege_type='UPDATE'),
  0, 'the control role cannot rewrite the singleton key -- column-scoped'
);
-- Scoped to the capability roles, matching the provenance assertion
-- below. It cannot be scoped wider: the table owner always holds full
-- DML on its own table, and in Supabase `postgres` additionally carries
-- BYPASSRLS -- verified on the live project, not assumed -- so FORCE
-- ROW LEVEL SECURITY does not constrain it either. The boundary this
-- system actually rests on is that no application identity is ever
-- `postgres`; every runtime process authenticates as a `_proc` role.
-- Asserting "append-only for everyone" would assert something false and
-- would have to be weakened later, which is worse than stating the real
-- boundary here.
select is(
  (select count(*)::int from information_schema.role_table_grants
   where table_schema='trading' and table_name='system_state_history'
     and grantee like '%\_svc'
     and privilege_type in ('UPDATE','DELETE')),
  0, 'no capability role may UPDATE or DELETE system_state_history'
);
-- The RLS half of append-only: no UPDATE or DELETE policy exists at all,
-- so for every RLS-subject identity those commands match no rows.
select is(
  (select count(*)::int from pg_policies
   where schemaname='trading' and tablename='system_state_history'
     and cmd in ('UPDATE','DELETE')),
  0, 'no UPDATE or DELETE policy exists on system_state_history'
);

-- ---------------------------------------------------------------------
-- Provenance spine is append-only: no UPDATE/DELETE anywhere.
-- Provenance you can rewrite is not provenance.
-- ---------------------------------------------------------------------
select is(
  (select count(*)::int from information_schema.role_table_grants
   where table_schema='trading' and grantee like '%\_svc'
     and privilege_type in ('UPDATE','DELETE')
     and table_name in ('component_versions','model_versions','runs','artifacts','artifact_edges')),
  0, 'no capability role may UPDATE or DELETE any provenance table'
);

-- ---------------------------------------------------------------------
-- The analyst / reviewer separation, asserted behaviourally.
--
-- The role switch goes INSIDE the SQL under test, never around the
-- assertion. pgTAP lives in a schema the capability roles hold no USAGE
-- on -- deliberately, since production has no pgTAP and a role must not
-- gain a grant merely to be tested -- so calling `lives_ok` while a
-- capability role is active fails to resolve pgTAP itself with
-- "function lives_ok(unknown, unknown) does not exist". That aborts the
-- plan rather than failing an assertion, which is how it hid.
--
-- The trailing `reset role` belongs INSIDE the executed string too, for
-- the same reason. `lives_ok` runs the statement and then calls `ok()`
-- to record the result -- still inside the role, since SET LOCAL in a
-- function without its own SET clause persists to end of transaction.
-- Resetting after the assertion returns is too late: `ok()` has already
-- failed to resolve. `throws_ok` escapes this only incidentally, because
-- the caught exception rolls the subtransaction back and reverts the
-- role with it. Verified against a real cluster, both ways.
-- ---------------------------------------------------------------------
create function public.as_role(role_name text, stmt text) returns text
  language sql immutable as
  $$ select 'set local role ' || quote_ident($1) || '; ' || $2 || '; reset role' $$;

set local role postgres;
insert into trading.component_versions (id, component, version, config_digest)
  values ('11111111-1111-1111-1111-111111111111','pgtap','v1','d');
insert into trading.runs (id, component_version_id, trigger)
  values ('22222222-2222-2222-2222-222222222222','11111111-1111-1111-1111-111111111111','pgtap');

select lives_ok(
  public.as_role('analyst_svc',
  $$insert into trading.artifacts (stage, run_id)
    values ('ai_analysis','22222222-2222-2222-2222-222222222222')$$),
  'analyst may author an ai_analysis'
);
select throws_ok(
  public.as_role('analyst_svc',
  $$insert into trading.artifacts (stage, run_id)
    values ('adversarial_review','22222222-2222-2222-2222-222222222222')$$),
  '42501', NULL,
  'analyst may NOT author the adversarial review of its own work'
);
select throws_ok(
  public.as_role('analyst_svc',
  $$update trading.system_state set state='paused' where singleton$$),
  NULL, NULL, 'analyst may not touch the kill switch'
);

select lives_ok(
  public.as_role('reviewer_svc',
  $$insert into trading.artifacts (stage, run_id)
    values ('adversarial_review','22222222-2222-2222-2222-222222222222')$$),
  'reviewer may author an adversarial_review'
);
select throws_ok(
  public.as_role('reviewer_svc',
  $$insert into trading.artifacts (stage, run_id)
    values ('ai_analysis','22222222-2222-2222-2222-222222222222')$$),
  '42501', NULL,
  'reviewer may NOT author the analysis it reviews'
);

select lives_ok(
  public.as_role('md_ingest_svc',
  $$insert into trading.artifacts (stage, run_id, observed_at, captured_at)
    values ('market_observation','22222222-2222-2222-2222-222222222222', now(), now())$$),
  'md_ingest may author a market_observation'
);
select throws_ok(
  public.as_role('md_ingest_svc',
  $$insert into trading.artifacts (stage, run_id)
    values ('feature','22222222-2222-2222-2222-222222222222')$$),
  '42501', NULL, 'md_ingest may not author a feature'
);

select lives_ok(
  public.as_role('feature_engine_svc',
  $$insert into trading.artifacts (stage, run_id)
    values ('feature','22222222-2222-2222-2222-222222222222')$$),
  'feature_engine may author a feature'
);
select throws_ok(
  public.as_role('feature_engine_svc',
  $$update trading.system_state set state='paused' where singleton$$),
  NULL, NULL, 'feature_engine may not touch the kill switch'
);

select lives_ok(
  public.as_role('trading_control_svc',
  $$update trading.system_state set state='paused', changed_by='pgtap' where singleton$$),
  'the control role CAN pause the system'
);
select lives_ok(
  public.as_role('trading_control_svc',
  $$update trading.system_state set state='normal', changed_by='pgtap' where singleton$$),
  'the control role CAN resume the system'
);
select throws_ok(
  public.as_role('trading_control_svc',
  $$insert into trading.artifacts (stage, run_id)
    values ('feature','22222222-2222-2222-2222-222222222222')$$),
  NULL, NULL, 'the control role does no pipeline work'
);

select lives_ok(
  public.as_role('delivery_svc',
  $$select state from trading.system_state$$),
  'every pipeline identity can read the kill switch'
);

-- ---------------------------------------------------------------------
-- Structural: the schema contains EXACTLY the foundation tables and
-- nothing else.
--
-- Expressed as an allow-list rather than a deny-list of the retired
-- system's organizational tables. That is deliberately stronger: a
-- deny-list only catches the nine names someone thought to write down,
-- while this catches any unexpected table -- an organizational
-- abstraction under a new name, a premature pipeline table, or a
-- forgotten scratch table.
-- ---------------------------------------------------------------------
select set_eq(
  $$select table_name::text from information_schema.tables where table_schema='trading'$$,
  $$values ('system_state'),('system_state_history'),('component_versions'),
           ('model_versions'),('runs'),('artifacts'),('artifact_edges'),
           ('feature_configs'),('features')$$,
  'the trading schema holds exactly the foundation and feature-store tables'
);

-- ---------------------------------------------------------------------
-- T-003 feature store.
-- ---------------------------------------------------------------------
select is(
  (select count(*)::int from information_schema.role_table_grants
   where table_schema='trading' and table_name in ('features','feature_configs')
     and grantee like '%\_svc'
     and privilege_type in ('UPDATE','DELETE')),
  0, 'the feature store is append-only for every capability role'
);
select is(
  (select count(*)::int from information_schema.role_table_grants
   where table_schema='trading' and table_name='features'
     and privilege_type='INSERT' and grantee like '%\_svc'
     and grantee <> 'feature_engine_svc'),
  0, 'only the feature engine may write features'
);
-- The constraint is the lookahead invariant, enforced below the Python
-- layer so a future writer cannot bypass it.
select throws_ok(
  public.as_role('postgres',
  $$insert into trading.features
      (run_id, config_id, instrument_symbol, kind, type, session_date,
       effective_at, available_at, inputs_digest)
    values ('22222222-2222-2222-2222-222222222222',
            '33333333-3333-3333-3333-333333333333',
            'NQZ6','feature','vwap','2026-01-12',
            '2026-01-12T15:00:00Z','2026-01-12T14:59:00Z','d')$$),
  '23514', NULL,
  'a feature knowable before it is true is rejected by the database'
);
select is(
  (select count(*)::int from pg_constraint c
   join pg_namespace n on n.oid=c.connamespace
   where n.nspname='trading' and c.contype='f'
     and c.confrelid::regclass::text like '%task%'),
  0, 'nothing in the provenance chain depends on an orchestration table'
);

select * from finish();
rollback;
