-- T-001 -- capability identities, grants and RLS policies.
--
-- Two-tier NOLOGIN/LOGIN pattern, carried over from the retired system
-- because it earned its place: a NOLOGIN group role holds every grant, a
-- NOLOGIN process role inherits it, and a LOGIN credential is provisioned
-- per-process immediately before that process deploys -- never in a
-- migration. Rotating a credential then touches no grant and no policy.
--
-- NOTHING HERE PROVISIONS A USABLE CREDENTIAL. Every role below is NOLOGIN
-- with no password.
--
-- On capability declarations (T-001 §12): this project deliberately has
-- NO capability registry. The retired system had one, and it silently
-- drifted from reality -- its trading declaration still claimed the tables
-- did not exist long after they shipped, and its own drift detector could
-- not see scope-content divergence. Actual grants ARE the authority, and
-- CI verifies them directly against this matrix. A declaration that can
-- diverge from effective authority is worse than none, because it invites
-- false confidence. Compose, don't build.

-- ---------------------------------------------------------------------
-- 1. Capability identities. One per pipeline stage plus control.
--
--    analyst and reviewer are SEPARATE identities, not a convenience
--    split: an adversarial review whose author can write the thing it
--    reviews is not adversarial. The database enforces that, not a code
--    convention.
-- ---------------------------------------------------------------------
create role md_ingest_svc        nologin;
create role feature_engine_svc   nologin;
create role setup_detector_svc   nologin;
create role analyst_svc          nologin;
create role reviewer_svc         nologin;
create role delivery_svc         nologin;
create role trading_control_svc  nologin;

create role md_ingest_proc       nologin in role md_ingest_svc;
create role feature_engine_proc  nologin in role feature_engine_svc;
create role setup_detector_proc  nologin in role setup_detector_svc;
create role analyst_proc         nologin in role analyst_svc;
create role reviewer_proc        nologin in role reviewer_svc;
create role delivery_proc        nologin in role delivery_svc;
create role trading_control_proc nologin in role trading_control_svc;

-- `SET ROLE` requires explicit membership -- it is not implied by
-- ownership or CREATEROLE. The pgTAP strategy (SET ROLE, never a real
-- LOGIN) depends on this. It grants `postgres` nothing it lacks as owner.
grant md_ingest_svc, feature_engine_svc, setup_detector_svc, analyst_svc,
      reviewer_svc, delivery_svc, trading_control_svc
  to postgres;

-- Schema visibility. USAGE only: it permits naming objects, never reaching
-- their contents -- table grants and RLS decide that.
grant usage on schema trading to
  md_ingest_svc, feature_engine_svc, setup_detector_svc, analyst_svc,
  reviewer_svc, delivery_svc, trading_control_svc;


-- ---------------------------------------------------------------------
-- 2. Kill switch grants.
--
--    Every runtime identity reads it. Exactly one identity writes it, and
--    that identity runs no pipeline work. No application component can
--    turn itself back on -- the property the whole mechanism exists for.
-- ---------------------------------------------------------------------
grant select on trading.system_state to
  md_ingest_svc, feature_engine_svc, setup_detector_svc, analyst_svc,
  reviewer_svc, delivery_svc, trading_control_svc;

-- Column-scoped, excluding `singleton`: the writer may change the state,
-- never the row's identity.
grant update (state, changed_by, reason, changed_at) on trading.system_state
  to trading_control_svc;
grant insert (state, changed_by, reason) on trading.system_state_history
  to trading_control_svc;
grant select on trading.system_state_history to trading_control_svc;

create policy read_state on trading.system_state for select
  using (
    pg_has_role(current_user, 'md_ingest_svc', 'member')
    or pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
    or pg_has_role(current_user, 'trading_control_svc', 'member')
  );

create policy control_update_state on trading.system_state for update
  using (pg_has_role(current_user, 'trading_control_svc', 'member'))
  with check (pg_has_role(current_user, 'trading_control_svc', 'member'));

create policy control_read_history on trading.system_state_history for select
  using (pg_has_role(current_user, 'trading_control_svc', 'member'));
create policy control_insert_history on trading.system_state_history for insert
  with check (pg_has_role(current_user, 'trading_control_svc', 'member'));


-- ---------------------------------------------------------------------
-- 3. Provenance spine grants.
--
--    Every pipeline identity may register the version it is running as
--    and open its own runs -- provenance must never be something a
--    component can skip because it lacks permission to record itself.
--    Nobody gets UPDATE or DELETE anywhere in the spine: provenance is
--    append-only, or it is not provenance.
-- ---------------------------------------------------------------------
grant select on trading.component_versions, trading.model_versions,
                trading.runs, trading.artifacts, trading.artifact_edges
  to md_ingest_svc, feature_engine_svc, setup_detector_svc, analyst_svc,
     reviewer_svc, delivery_svc;

grant insert (component, version, config_digest, source_ref, description)
  on trading.component_versions
  to md_ingest_svc, feature_engine_svc, setup_detector_svc, analyst_svc,
     reviewer_svc, delivery_svc;

-- Only the model-using stages register model versions.
grant insert (provider, model, prompt_version, parameters_digest)
  on trading.model_versions to analyst_svc, reviewer_svc;

grant insert (component_version_id, model_version_id, trigger, started_at, finished_at, outcome, detail)
  on trading.runs
  to md_ingest_svc, feature_engine_svc, setup_detector_svc, analyst_svc,
     reviewer_svc, delivery_svc;

-- `id` is grantable on artifacts because a producer must be able to
-- declare lineage edges referencing a row it just wrote, and RETURNING
-- requires SELECT privilege on the returned column. All these roles have
-- SELECT here, so client-side ids are a convenience rather than a
-- necessity -- but the grant keeps edge-writing a single round trip.
grant insert (id, stage, run_id, source_ref, observed_at, captured_at, generated_at, payload_digest)
  on trading.artifacts
  to md_ingest_svc, feature_engine_svc, setup_detector_svc, analyst_svc,
     reviewer_svc, delivery_svc;

grant insert (parent_artifact_id, child_artifact_id) on trading.artifact_edges
  to feature_engine_svc, setup_detector_svc, analyst_svc, reviewer_svc, delivery_svc;

create policy pipeline_read_component_versions on trading.component_versions for select
  using (
    pg_has_role(current_user, 'md_ingest_svc', 'member')
    or pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
  );
create policy pipeline_write_component_versions on trading.component_versions for insert
  with check (
    pg_has_role(current_user, 'md_ingest_svc', 'member')
    or pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
  );

create policy pipeline_read_model_versions on trading.model_versions for select
  using (
    pg_has_role(current_user, 'md_ingest_svc', 'member')
    or pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
  );
create policy model_stages_write_model_versions on trading.model_versions for insert
  with check (
    pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
  );

create policy pipeline_read_runs on trading.runs for select
  using (
    pg_has_role(current_user, 'md_ingest_svc', 'member')
    or pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
  );
create policy pipeline_write_runs on trading.runs for insert
  with check (
    pg_has_role(current_user, 'md_ingest_svc', 'member')
    or pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
  );

-- Reads across the whole chain are open to every pipeline identity: an
-- adversarial reviewer that cannot see the evidence beneath an analysis
-- cannot review it, and a feature engine benefits from seeing what it
-- produced before. Isolation here is on WRITES.
create policy pipeline_read_artifacts on trading.artifacts for select
  using (
    pg_has_role(current_user, 'md_ingest_svc', 'member')
    or pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
  );

-- THE STAGE BOUNDARY. Each identity may only write artifacts belonging to
-- its own stage. This is what makes the analyst/reviewer separation real:
-- the analyst cannot author an adversarial_review, and the reviewer cannot
-- author the ai_analysis it is meant to critique -- enforced by the
-- database, not by whoever writes the calling code.
create policy stage_scoped_artifact_write on trading.artifacts for insert
  with check (
    (stage = 'market_observation'  and pg_has_role(current_user, 'md_ingest_svc', 'member'))
    or (stage = 'feature'          and pg_has_role(current_user, 'feature_engine_svc', 'member'))
    or (stage = 'market_state'     and pg_has_role(current_user, 'feature_engine_svc', 'member'))
    or (stage = 'research'         and pg_has_role(current_user, 'setup_detector_svc', 'member'))
    or (stage = 'setup_candidate'  and pg_has_role(current_user, 'setup_detector_svc', 'member'))
    or (stage = 'entry_candidate'  and pg_has_role(current_user, 'setup_detector_svc', 'member'))
    or (stage = 'ai_analysis'      and pg_has_role(current_user, 'analyst_svc', 'member'))
    or (stage = 'adversarial_review' and pg_has_role(current_user, 'reviewer_svc', 'member'))
    or (stage = 'delivery'         and pg_has_role(current_user, 'delivery_svc', 'member'))
  );

create policy pipeline_read_edges on trading.artifact_edges for select
  using (
    pg_has_role(current_user, 'md_ingest_svc', 'member')
    or pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
  );
create policy pipeline_write_edges on trading.artifact_edges for insert
  with check (
    pg_has_role(current_user, 'feature_engine_svc', 'member')
    or pg_has_role(current_user, 'setup_detector_svc', 'member')
    or pg_has_role(current_user, 'analyst_svc', 'member')
    or pg_has_role(current_user, 'reviewer_svc', 'member')
    or pg_has_role(current_user, 'delivery_svc', 'member')
  );
