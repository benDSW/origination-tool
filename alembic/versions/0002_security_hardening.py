"""Security hardening: lock owners, a race-safe lookup budget, erasure suppressions
and server-side admin sessions.

- ``locks.owner``: a random per-holder token, so only the run holding the lock can
  heartbeat or release it.
- ``lookup_budget``: paid lookups attempted per provider per UTC day, reserved
  before each RocketReach call (the daily cap can no longer be bypassed).
- ``suppressions``: HMAC hashes of identifiers of erased founders (no PII), so a
  forgotten founder is never re-ingested.
- ``admin_sessions``: SHA-256 of live dashboard session ids, so logout and
  "log out everywhere" revoke a session on the server.

Hand-written and frozen, like 0001.

Revision ID: 0002_security_hardening
Revises: 0001_initial
Create Date: 2026-10-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_security_hardening"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMAIL_SOURCE = ("evertrace", "rocketreach", "manual")


def _ts() -> sa.DateTime:
    return sa.DateTime(timezone=False)


def upgrade() -> None:
    with op.batch_alter_table("locks") as batch:
        batch.add_column(sa.Column("owner", sa.String(length=64), nullable=True))

    op.create_table(
        "lookup_budget",
        sa.Column(
            "provider",
            sa.Enum(*EMAIL_SOURCE, name="emailsource", native_enum=False, length=32, create_constraint=False),
            nullable=False,
        ),
        sa.Column("day_start", _ts(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "day_start", name="pk_lookup_budget"),
    )

    op.create_table(
        "suppressions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_suppressions"),
        sa.UniqueConstraint("key_hash", name="uq_suppressions_key_hash"),
    )

    op.create_table(
        "admin_sessions",
        sa.Column("sid_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("expires_at", _ts(), nullable=False),
        sa.PrimaryKeyConstraint("sid_hash", name="pk_admin_sessions"),
    )
    op.create_index("ix_admin_sessions_expires_at", "admin_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_sessions_expires_at", table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.drop_table("suppressions")
    op.drop_table("lookup_budget")
    with op.batch_alter_table("locks") as batch:
        batch.drop_column("owner")
