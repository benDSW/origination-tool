"""Keep every verified RocketReach address per lookup, not just the top pick.

- ``email_lookups.candidates``: JSON list of ``{email, grade, smtp_valid, reason}``,
  best first, so the dashboard can offer alternatives when the top pick is a
  former employer's address. NULL for lookups made before this migration.

Hand-written and frozen, like 0001.

Revision ID: 0003_email_candidates
Revises: 0002_security_hardening
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_email_candidates"
down_revision: str | None = "0002_security_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("email_lookups") as batch:
        batch.add_column(sa.Column("candidates", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("email_lookups") as batch:
        batch.drop_column("candidates")
