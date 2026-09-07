-- Database security assertions, run against an EPHEMERAL CI stack.
-- pgTAP is never installed in the production application schema.
--
-- These test BEHAVIOUR (can this role actually do this?) rather than
-- configuration text wherever a stronger check is practical.

begin;
select plan(26);

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
select is(
  (select count(*)::int from information_schema.role_table_grants
   where table_schema='trading' and table_name='system_state_history'
     and privilege_type in ('UPDATE','DELETE')),
  0, 'system_state_history is append-only for everyone'
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
-- ---------------------------------------------------------------------
set local role postgres;
insert into trading.component_versions (id, component, version, config_digest)
  values ('11111111-1111-1111-1111-111111111111','pgtap','v1','d');
insert into trading.runs (id, component_version_id, trigger)
  values ('22222222-2222-2222-2222-222222222222','11111111-1111-1111-1111-111111111111','pgtap');

set local role analyst_svc;
select lives_ok(
  $$insert into trading.artifacts (stage, run_id)
    values ('ai_analysis','22222222-2222-2222-2222-222222222222')$$,
  'analyst may author an ai_analysis'
);
select throws_ok(
  $$insert into trading.artifacts (stage, run_id)
    values ('adversarial_review','22222222-2222-2222-2222-222222222222')$$,
  '42501', NULL,
  'analyst may NOT author the adversarial review of its own work'
);
select throws_ok(
  $$update trading.system_state set state='paused' where singleton$$,
  NULL, NULL, 'analyst may not touch the kill switch'
);
reset role;

set local role reviewer_svc;
select lives_ok(
  $$insert into trading.artifacts (stage, run_id)
    values ('adversarial_review','22222222-2222-2222-2222-222222222222')$$,
  'reviewer may author an adversarial_review'
);
select throws_ok(
  $$insert into trading.artifacts (stage, run_id)
    values ('ai_analysis','22222222-2222-2222-2222-222222222222')$$,
  '42501', NULL,
  'reviewer may NOT author the analysis it reviews'
);
reset role;

set local role md_ingest_svc;
select lives_ok(
  $$insert into trading.artifacts (stage, run_id, observed_at, captured_at)
    values ('market_observation','22222222-2222-2222-2222-222222222222', now(), now())$$,
  'md_ingest may author a market_observation'
);
select throws_ok(
  $$insert into trading.artifacts (stage, run_id)
    values ('feature','22222222-2222-2222-2222-222222222222')$$,
  '42501', NULL, 'md_ingest may not author a feature'
);
reset role;

set local role feature_engine_svc;
select lives_ok(
  $$insert into trading.artifacts (stage, run_id)
    values ('feature','22222222-2222-2222-2222-222222222222')$$,
  'feature_engine may author a feature'
);
select throws_ok(
  $$update trading.system_state set state='paused' where singleton$$,
  NULL, NULL, 'feature_engine may not touch the kill switch'
);
reset role;

set local role trading_control_svc;
select lives_ok(
  $$update trading.system_state set state='paused', changed_by='pgtap' where singleton$$,
  'the control role CAN pause the system'
);
select lives_ok(
  $$update trading.system_state set state='normal', changed_by='pgtap' where singleton$$,
  'the control role CAN resume the system'
);
select throws_ok(
  $$insert into trading.artifacts (stage, run_id)
    values ('feature','22222222-2222-2222-2222-222222222222')$$,
  NULL, NULL, 'the control role does no pipeline work'
);
reset role;

set local role delivery_svc;
select lives_ok(
  $$select state from trading.system_state$$,
  'every pipeline identity can read the kill switch'
);
reset role;

-- ---------------------------------------------------------------------
-- Structural: none of the retired organizational tables exist here.
-- ---------------------------------------------------------------------
select is(
  (select count(*)::int from information_schema.tables
   where table_schema='trading'
     and table_name in ('tasks','objectives','initiatives','departments',
                        'attention_queue','approval_queue','context_facts',
                        'conversation_turns','capability_policies')),
  0, 'no Command Center organizational abstraction was recreated'
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
