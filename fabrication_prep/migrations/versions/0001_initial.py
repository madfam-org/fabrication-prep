"""Initial schema: slice jobs (a Postgres job queue), content-addressed artifacts, outbox.

Queue model
-----------
* ``slice_jobs.status``: ``queued`` -> ``running`` -> ``succeeded`` | ``failed`` (permanent error) |
  ``dead_lettered`` (attempts exhausted). A transient failure returns the job to ``queued`` with a later
  ``run_after``. Workers claim with ``FOR UPDATE SKIP LOCKED`` and hold a lease (``lease_owner``,
  ``lease_expires_at``) they renew while slicing; an expired lease is reaped back to ``queued`` or, with no
  attempts left, to ``dead_lettered``.
* Every terminal transition writes an ``outbox`` row in the same transaction.

Visibility
----------
* ``slice_jobs`` has FORCE ROW LEVEL SECURITY: a job is visible to its owner (the caller's ``sub``,
  setting ``fabrication_prep.owner``) or to the worker context (``fabrication_prep.worker = 'on'``).
* ``artifacts`` rows are content metadata (sha256, size, media type); bytes leave only through signed URLs.
* The runtime role may insert into ``outbox`` but not read or change it; the relay runs as the owner.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-03
"""

from __future__ import annotations

import re

from alembic import context, op

from fabrication_prep.settings import get_settings

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

OWNER = "current_setting('fabrication_prep.owner', true)"
WORKER = "coalesce(current_setting('fabrication_prep.worker', true), '') = 'on'"
VISIBLE = f"(owner_sub = {OWNER} OR {WORKER})"


def _app_role() -> str:
    role = context.config.attributes.get("app_db_role") or get_settings().app_db_role
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise RuntimeError("APP_DB_ROLE must be a lower-case SQL identifier")
    return role


def upgrade() -> None:
    role = _app_role()
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
            RAISE EXCEPTION 'runtime role {role} does not exist; create it first (docs/operator-setup.md)';
          END IF;
        END $$;

        CREATE TABLE artifacts (
          sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
          media_type text NOT NULL CHECK (length(media_type) BETWEEN 3 AND 200),
          bytes bigint NOT NULL CHECK (bytes >= 0),
          filename text NOT NULL CHECK (filename ~ '^[A-Za-z0-9._-]{{1,200}}$'),
          created_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE slice_jobs (
          id uuid PRIMARY KEY,
          owner_sub text NOT NULL CHECK (length(owner_sub) BETWEEN 1 AND 200),
          tenant_id text CHECK (tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 200),
          idempotency_key text CHECK (idempotency_key IS NULL OR length(idempotency_key) BETWEEN 1 AND 200),
          request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{{64}}$'),
          request jsonb NOT NULL,
          target text NOT NULL CHECK (target IN ('klipper_gcode', 'bambu_3mf')),
          status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead_lettered')),
          attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
          max_attempts integer NOT NULL CHECK (max_attempts >= 1),
          run_after timestamptz NOT NULL DEFAULT now(),
          lease_owner text,
          lease_expires_at timestamptz,
          error_code text,
          error_message text CHECK (error_message IS NULL OR length(error_message) <= 2000),
          output_sha256 text REFERENCES artifacts (sha256),
          slicer_variables_sha256 text REFERENCES artifacts (sha256),
          result jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          started_at timestamptz,
          finished_at timestamptz,
          UNIQUE (owner_sub, idempotency_key),
          CHECK ((status = 'running') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)),
          CHECK (status <> 'succeeded' OR (output_sha256 IS NOT NULL AND slicer_variables_sha256 IS NOT NULL))
        );
        CREATE INDEX slice_jobs_queued ON slice_jobs (run_after, created_at) WHERE status = 'queued';
        CREATE INDEX slice_jobs_leases ON slice_jobs (lease_expires_at) WHERE status = 'running';

        CREATE TABLE outbox (
          id bigserial PRIMARY KEY,
          topic text NOT NULL CHECK (topic IN ('slice_job.succeeded', 'slice_job.failed', 'slice_job.dead_lettered')),
          aggregate_id uuid NOT NULL,
          payload jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          published_at timestamptz
        );

        ALTER TABLE slice_jobs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE slice_jobs FORCE ROW LEVEL SECURITY;
        CREATE POLICY slice_jobs_visible ON slice_jobs FOR ALL USING {VISIBLE} WITH CHECK {VISIBLE};

        ALTER TABLE outbox ENABLE ROW LEVEL SECURITY;
        ALTER TABLE outbox FORCE ROW LEVEL SECURITY;
        CREATE POLICY outbox_insert ON outbox FOR INSERT TO {role} WITH CHECK ({WORKER});
        CREATE POLICY outbox_owner_read ON outbox FOR SELECT TO CURRENT_USER USING (true);
        CREATE POLICY outbox_owner_mark ON outbox FOR UPDATE TO CURRENT_USER USING (true) WITH CHECK (true);
        """
    )
    # Least privilege: no DELETE, TRUNCATE or DDL for the runtime role; outbox is insert-only.
    op.execute(
        f"""
        REVOKE ALL ON slice_jobs, artifacts, outbox FROM PUBLIC;
        GRANT USAGE ON SCHEMA public TO {role};
        GRANT SELECT, INSERT, UPDATE ON slice_jobs TO {role};
        GRANT SELECT, INSERT ON artifacts TO {role};
        GRANT INSERT ON outbox TO {role};
        GRANT USAGE ON SEQUENCE outbox_id_seq TO {role};
        GRANT SELECT ON alembic_version TO {role};
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS outbox, slice_jobs, artifacts CASCADE;")
