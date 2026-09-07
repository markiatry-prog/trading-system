-- T-003: the feature store.
--
-- Two tables, both append-only, both hanging off the T-001 provenance
-- spine. `feature_configs` records the parameter set a run used;
-- `features` records what the engine emitted. Splitting them means a
-- configuration is stated once and referenced, rather than repeated on
-- every row and liable to disagree with itself.
--
-- THE TWO TIMES ARE BOTH STORED AND BOTH INDEXED.
--   effective_at  when the fact became true in market time
--   available_at  the earliest instant it could have been known
-- Research MUST filter on available_at. Filtering on effective_at
-- silently introduces lookahead and produces results that look
-- excellent, which is the failure mode worth designing against.

-- ---------------------------------------------------------------------
-- Configurations
-- ---------------------------------------------------------------------
create table trading.feature_configs (
  id            uuid primary key default gen_random_uuid(),
  name          text        not null,
  digest        text        not null,
  engine_version text       not null,
  parameters    jsonb       not null,
  created_at    timestamptz not null default now(),
  unique (digest, engine_version)
);

comment on table trading.feature_configs is
  'One row per distinct parameter set. The digest is what makes "did the '
  'rule change or did the parameter change?" answerable after the fact.';

-- ---------------------------------------------------------------------
-- Feature and event records
-- ---------------------------------------------------------------------
create type trading.feature_record_kind as enum ('feature', 'event');

create table trading.features (
  id                bigint generated always as identity primary key,
  run_id            uuid        not null references trading.runs(id),
  config_id         uuid        not null references trading.feature_configs(id),
  instrument_symbol text        not null,
  kind              trading.feature_record_kind not null,
  type              text        not null,
  session_date      date        not null,
  effective_at      timestamptz not null,
  available_at      timestamptz not null,
  value             numeric,
  state             text,
  attributes        jsonb       not null default '{}'::jsonb,
  inputs_digest     text        not null,
  created_at        timestamptz not null default now(),

  -- A fact cannot be knowable before it is true. Enforced in the
  -- database as well as the type, because this is the invariant the
  -- entire lookahead defence rests on and it must survive any future
  -- writer that bypasses the Python layer.
  constraint available_not_before_effective check (available_at >= effective_at)
);

comment on column trading.features.available_at is
  'Earliest instant this fact could have been known. Research queries '
  'MUST filter on this column, never on effective_at.';

-- Research access patterns: "what was knowable by T", and "all records
-- of this type for this instrument over a date range".
create index features_available_at_idx
  on trading.features (instrument_symbol, available_at);
create index features_type_session_idx
  on trading.features (instrument_symbol, type, session_date);
create index features_run_idx on trading.features (run_id);

-- ---------------------------------------------------------------------
-- RLS: enabled AND forced, as with every table in this schema.
-- ---------------------------------------------------------------------
alter table trading.feature_configs enable row level security;
alter table trading.feature_configs force  row level security;
alter table trading.features        enable row level security;
alter table trading.features        force  row level security;

revoke all on trading.feature_configs from anon, authenticated;
revoke all on trading.features        from anon, authenticated;

-- The feature engine is the only writer. Every capability may read:
-- features are the shared substrate the later stages reason over.
grant select on trading.feature_configs to
  md_ingest_svc, feature_engine_svc, setup_detector_svc, analyst_svc,
  reviewer_svc, delivery_svc, trading_control_svc;
grant select on trading.features to
  md_ingest_svc, feature_engine_svc, setup_detector_svc, analyst_svc,
  reviewer_svc, delivery_svc, trading_control_svc;

-- Column-scoped, excluding the identity and the server-set timestamp.
grant insert (name, digest, engine_version, parameters)
  on trading.feature_configs to feature_engine_svc;
grant insert (run_id, config_id, instrument_symbol, kind, type, session_date,
              effective_at, available_at, value, state, attributes, inputs_digest)
  on trading.features to feature_engine_svc;

create policy pipeline_read_feature_configs on trading.feature_configs
  for select using (
    pg_has_role(current_user, 'md_ingest_svc', 'member')
    or pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
    or pg_has_role(current_user, 'trading_control_svc', 'member')
  );
create policy feature_engine_writes_configs on trading.feature_configs
  for insert with check (pg_has_role(current_user, 'feature_engine_svc', 'member'));

create policy pipeline_read_features on trading.features
  for select using (
    pg_has_role(current_user, 'md_ingest_svc', 'member')
    or pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
    or pg_has_role(current_user, 'trading_control_svc', 'member')
  );
-- Only the feature engine writes features. The analyst and the
-- adversarial reviewer read them and can never author them, which is the
-- same separation the artifact stages enforce.
create policy feature_engine_writes_features on trading.features
  for insert with check (pg_has_role(current_user, 'feature_engine_svc', 'member'));
