"""Artifact retention: each artifact remembers when a job last produced it, and when its bytes were expired.

* ``artifacts.last_used_at`` — set when the row is first written and again whenever a later job produces the same
  bytes (content addressing deduplicates them). Existing rows start from ``created_at``.
* ``artifacts.expired_at`` — set by the worker's retention sweep when it deletes the bytes from the store; cleared
  again if a later job produces the same bytes (the worker re-uploads them first).
* The runtime role gets ``UPDATE`` on exactly those two columns. It still cannot delete a row: job rows keep their
  digests (pravara's passport records them) after the bytes are gone.

Revision ID: 0002_artifact_retention
Revises: 0001_initial
Create Date: 2026-10-05
"""

from __future__ import annotations

import re

from alembic import context, op

from fabrication_prep.settings import get_settings

revision = "0002_artifact_retention"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _app_role() -> str:
    role = context.config.attributes.get("app_db_role") or get_settings().app_db_role
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise RuntimeError("APP_DB_ROLE must be a lower-case SQL identifier")
    return role


def upgrade() -> None:
    role = _app_role()
    op.execute(
        """
        ALTER TABLE artifacts ADD COLUMN last_used_at timestamptz NOT NULL DEFAULT now();
        UPDATE artifacts SET last_used_at = created_at;
        ALTER TABLE artifacts ADD COLUMN expired_at timestamptz;
        ALTER TABLE artifacts ADD CONSTRAINT artifacts_expired_after_use
          CHECK (expired_at IS NULL OR expired_at >= last_used_at);
        CREATE INDEX artifacts_retention ON artifacts (last_used_at) WHERE expired_at IS NULL;
        """
    )
    op.execute(f"GRANT UPDATE (last_used_at, expired_at) ON artifacts TO {role};")


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS artifacts_retention;
        ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS artifacts_expired_after_use;
        ALTER TABLE artifacts DROP COLUMN IF EXISTS expired_at;
        ALTER TABLE artifacts DROP COLUMN IF EXISTS last_used_at;
        """
    )
