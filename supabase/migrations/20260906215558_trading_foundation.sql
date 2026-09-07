-- T-001 -- trading system foundation: the `trading` schema, the fail-closed
-- kill switch, and the minimal provenance spine.
--
-- DESIGN BOUNDARY. This migration deliberately does NOT create feature,
-- market-state, setup, entry, analysis or review tables. Those belong to
-- their own tickets. What it creates is the spine those stages will attach
-- to, so that every future artifact can be traced back to the exact
-- implementation, configuration and inputs that produced it -- without any
-- stage having to invent its own provenance scheme, and without the
-- organizational coupling the retired system carried, in which the
-- interpretation layer was chained by NOT NULL foreign keys all the way
-- up to an orchestration table only the orchestrator could write -- so no
-- interpretation could be recorded without the orchestrator acting first.
-- See docs/LEGACY-REUSE.md for the full description.
--
-- Nothing here references, imports, or depends on that system.

create schema if not exists trading;

-- Application tables never live in `public`: PostgREST exposes `public` by
-- default, and this project's API surface must stay empty.
revoke all on schema public from anon, authenticated;
revoke all on schema trading from anon, authenticated;
alter default privileges in schema trading revoke all on tables from anon, authenticated;
alter default privileges in schema trading revoke all on sequences from anon, authenticated;
alter default privileges in schema trading revoke all on functions from anon, authenticated;
alter default privileges in schema public revoke all on tables from anon, authenticated;


-- ---------------------------------------------------------------------
-- 1. Kill switch. Same fail-closed contract the retired system proved,
--    rebuilt here as its own instance -- never shared, never reachable
--    from that project.
--
--    Two states only. A third state is a design decision, not a
--    convenience: every caller must be able to reduce the world to
--    "permitted" or "not permitted" without interpretation.
-- ---------------------------------------------------------------------
create type trading.system_state_value as enum ('normal', 'paused');

create table trading.system_state (
  singleton boolean primary key default true check (singleton),
  state trading.system_state_value not null default 'normal',
  changed_by text not null default 'human',
  reason text,
  changed_at timestamptz not null default now()
);

-- Append-only. No UPDATE or DELETE grant exists for anyone, so the
-- guarantee is structural rather than procedural.
create table trading.system_state_history (
  id uuid primary key default gen_random_uuid(),
  state trading.system_state_value not null,
  changed_by text not null,
  reason text,
  changed_at timestamptz not null default now()
);

insert into trading.system_state (singleton, state, changed_by, reason)
values (true, 'normal', 'migration',
        'Initial seed at T-001. Explicit, not relied upon as an implicit default.');


-- ---------------------------------------------------------------------
-- 2. Provenance spine.
--
--    The question this must answer years from now, without relying on
--    memory or undocumented code state:
--
--      "Did ORB v4 outperform ORB v3 in this regime?"
--
--    Which decomposes into: which implementation produced this artifact,
--    under which configuration, from which inputs, observed when, and
--    what did it feed?
-- ---------------------------------------------------------------------

-- 2a. Which implementation. One row per (component, version, config).
--     `config_digest` is a hash of the effective configuration, so two
--     runs of the same code under different parameters are distinguishable
--     without storing the parameters themselves in a shape we would have
--     to guess at now.
create table trading.component_versions (
  id uuid primary key default gen_random_uuid(),
  component text not null,
  version text not null,
  config_digest text not null,
  source_ref text,                       -- git SHA or equivalent
  description text,
  registered_at timestamptz not null default now(),
  unique (component, version, config_digest)
);

-- 2b. Which model, for stages where a model is involved. Kept separate
--     from component_versions because a deterministic component has no
--     model, and an AI stage has BOTH a component version (its harness,
--     its parsing, its guardrails) and a model version. Collapsing them
--     would make "same prompt, new model" indistinguishable from "new
--     prompt, same model".
create table trading.model_versions (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  model text not null,
  prompt_version text not null,
  parameters_digest text,
  registered_at timestamptz not null default now(),
  unique (provider, model, prompt_version, parameters_digest)
);

-- 2c. Which execution. The root of every provenance chain.
--
--     Note what is absent: no task_id, no objective_id, no department, no
--     assignment. A run is self-contained. This is the specific coupling
--     that made the retired system's interpretation layer unusable in
--     isolation, and it is deliberately not reproduced.
create type trading.run_outcome as enum ('completed', 'failed', 'blocked');

create table trading.runs (
  id uuid primary key default gen_random_uuid(),
  component_version_id uuid not null references trading.component_versions(id),
  model_version_id uuid references trading.model_versions(id),
  trigger text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  outcome trading.run_outcome,
  detail text
);

-- 2d. What was produced. A stage-tagged spine row.
--
--     Future stage tables (features, market_states, setup_candidates, ...)
--     each carry their own domain columns and a FK to artifacts.id. That
--     is the extension point: this table never needs to learn any stage's
--     shape, and no stage needs to reinvent lineage or timestamps.
--
--     The stage enum names the North Star pipeline without implementing
--     any of it.
--
--     THREE TIMESTAMPS, deliberately distinct -- the lesson the retired
--     system got right and the single most important thing carried over:
--       observed_at  -- when the underlying fact was true in the market
--       captured_at  -- when we received it
--       generated_at -- when this artifact was computed
--     A feature computed at 09:31 from a bar observed at 09:30 that we
--     received at 09:30:04 is three different times. Collapsing them makes
--     stale inputs indistinguishable from live ones, which is exactly the
--     error a deterministic feature engine must never make.
create type trading.artifact_stage as enum (
  'market_observation',
  'feature',
  'market_state',
  'research',
  'setup_candidate',
  'entry_candidate',
  'ai_analysis',
  'adversarial_review',
  'delivery'
);

create table trading.artifacts (
  id uuid primary key default gen_random_uuid(),
  stage trading.artifact_stage not null,
  run_id uuid not null references trading.runs(id),
  source_ref text,                       -- upstream source identifier, where applicable
  observed_at timestamptz,
  captured_at timestamptz,
  generated_at timestamptz not null default now(),
  payload_digest text
);

create index artifacts_stage_generated_idx on trading.artifacts (stage, generated_at desc);
create index artifacts_run_idx on trading.artifacts (run_id);

-- 2e. What fed what. An explicit edge table rather than a parent_id
--     column: a market state derives from many features, and an
--     adversarial review reads both the analysis and the evidence beneath
--     it. Single-parent lineage would be wrong on day one.
create table trading.artifact_edges (
  parent_artifact_id uuid not null references trading.artifacts(id),
  child_artifact_id uuid not null references trading.artifacts(id),
  primary key (parent_artifact_id, child_artifact_id),
  check (parent_artifact_id <> child_artifact_id)
);

create index artifact_edges_child_idx on trading.artifact_edges (child_artifact_id);


-- ---------------------------------------------------------------------
-- 3. RLS: ENABLE *and* FORCE on every application table.
--
--     FORCE is the deliberate difference from the retired system, which
--     used ENABLE only. ENABLE alone leaves the table owner exempt; FORCE
--     removes that exemption. This system carries money-adjacent data and
--     should not depend on "the owner never connects at runtime" being
--     true forever.
-- ---------------------------------------------------------------------
alter table trading.system_state          enable row level security;
alter table trading.system_state          force  row level security;
alter table trading.system_state_history  enable row level security;
alter table trading.system_state_history  force  row level security;
alter table trading.component_versions    enable row level security;
alter table trading.component_versions    force  row level security;
alter table trading.model_versions        enable row level security;
alter table trading.model_versions        force  row level security;
alter table trading.runs                  enable row level security;
alter table trading.runs                  force  row level security;
alter table trading.artifacts             enable row level security;
alter table trading.artifacts             force  row level security;
alter table trading.artifact_edges        enable row level security;
alter table trading.artifact_edges        force  row level security;
