"""Initial schema: searches, founders, outreach, audit, cache, runs, locks, templates.

Hand-written and frozen: enum values are inlined (not imported from the domain)
so later domain changes cannot silently rewrite history. Portable across
SQLite and Postgres: enums are VARCHAR(32), datetimes are naive UTC, JSON is
sa.JSON, and every constraint is explicitly named.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCATION_POLICY = ("uk_outside_london", "london_allowed")
OUTREACH_STATUS = (
    "discovered",
    "excluded",
    "needs_email",
    "drafted",
    "sent",
    "replied",
    "deck_received",
    "closed",
)
EXCLUSION_REASON = ("location", "in_affinity")
CLOSE_REASON = ("skipped", "declined", "lapsed", "not_raising")
EMAIL_SOURCE = ("evertrace", "rocketreach", "manual")
EVENT_SOURCE = ("pipeline", "auto:inbox", "manual")
LOOKUP_OUTCOME = ("found", "not_found", "rejected", "error")
RUN_TRIGGER = ("cron", "manual")
RUN_STATE = ("running", "succeeded", "partial", "failed", "skipped")


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, length=32, create_constraint=False)


def _ts() -> sa.DateTime:
    return sa.DateTime(timezone=False)


def upgrade() -> None:
    op.create_table(
        "searches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("location_policy", _enum("locationpolicy", LOCATION_POLICY), nullable=False),
        sa.Column("lookback_days", sa.Integer(), nullable=False),
        sa.Column("watermark", _ts(), nullable=True),
        sa.Column("last_synced_at", _ts(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_searches"),
        sa.UniqueConstraint("external_id", name="uq_searches_external_id"),
    )

    op.create_table(
        "founders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("evertrace_signal_id", sa.String(length=128), nullable=False),
        sa.Column("linkedin_slug", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("last_name", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("gender", sa.Text(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("signal_type", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("discovered_at", _ts(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("company_domain", sa.Text(), nullable=True),
        sa.Column("company_registry_url", sa.Text(), nullable=True),
        sa.Column("evertrace_email", sa.String(length=320), nullable=True),
        sa.Column("created_at", _ts(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_founders"),
        sa.UniqueConstraint("evertrace_signal_id", name="uq_founders_evertrace_signal_id"),
        sa.UniqueConstraint("linkedin_slug", name="uq_founders_linkedin_slug"),
    )

    op.create_table(
        "founder_searches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("founder_id", sa.Integer(), nullable=False),
        sa.Column("search_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["founder_id"],
            ["founders.id"],
            name="fk_founder_searches_founder_id_founders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["search_id"],
            ["searches.id"],
            name="fk_founder_searches_search_id_searches",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_founder_searches"),
        sa.UniqueConstraint("founder_id", "search_id", name="uq_founder_searches_founder_id_search_id"),
    )
    op.create_index("ix_founder_searches_search_id", "founder_searches", ["search_id"])

    op.create_table(
        "outreach",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("founder_id", sa.Integer(), nullable=False),
        sa.Column("status", _enum("outreachstatus", OUTREACH_STATUS), nullable=False),
        sa.Column("exclusion_reason", _enum("exclusionreason", EXCLUSION_REASON), nullable=True),
        sa.Column("exclusion_detail", sa.Text(), nullable=True),
        sa.Column("close_reason", _enum("closereason", CLOSE_REASON), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("email_source", _enum("emailsource", EMAIL_SOURCE), nullable=True),
        sa.Column("draft_subject", sa.Text(), nullable=True),
        sa.Column("draft_body", sa.Text(), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("updated_at", _ts(), nullable=False),
        sa.Column("drafted_at", _ts(), nullable=True),
        sa.Column("sent_at", _ts(), nullable=True),
        sa.Column("replied_at", _ts(), nullable=True),
        sa.Column("deck_received_at", _ts(), nullable=True),
        sa.Column("closed_at", _ts(), nullable=True),
        sa.Column("last_tracked_at", _ts(), nullable=True),
        sa.ForeignKeyConstraint(
            ["founder_id"], ["founders.id"], name="fk_outreach_founder_id_founders", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outreach"),
        sa.UniqueConstraint("founder_id", name="uq_outreach_founder_id"),
    )
    op.create_index("ix_outreach_status", "outreach", ["status"])
    op.create_index("ix_outreach_updated_at", "outreach", ["updated_at"])

    op.create_table(
        "status_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("outreach_id", sa.Integer(), nullable=False),
        sa.Column("from_status", _enum("outreachstatus", OUTREACH_STATUS), nullable=False),
        sa.Column("to_status", _enum("outreachstatus", OUTREACH_STATUS), nullable=False),
        sa.Column("source", _enum("eventsource", EVENT_SOURCE), nullable=False),
        sa.Column("at", _ts(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["outreach_id"], ["outreach.id"], name="fk_status_events_outreach_id_outreach", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_status_events"),
    )
    op.create_index("ix_status_events_outreach_id", "status_events", ["outreach_id"])

    op.create_table(
        "email_lookups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("founder_id", sa.Integer(), nullable=False),
        sa.Column("provider", _enum("emailsource", EMAIL_SOURCE), nullable=False),
        sa.Column("outcome", _enum("lookupoutcome", LOOKUP_OUTCOME), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("grade", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", _ts(), nullable=False),
        sa.ForeignKeyConstraint(
            ["founder_id"], ["founders.id"], name="fk_email_lookups_founder_id_founders", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_lookups"),
        sa.UniqueConstraint("founder_id", "provider", name="uq_email_lookups_founder_id_provider"),
    )
    op.create_index("ix_email_lookups_created_at", "email_lookups", ["created_at"])

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trigger", _enum("runtrigger", RUN_TRIGGER), nullable=False),
        sa.Column("state", _enum("runstate", RUN_STATE), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("started_at", _ts(), nullable=False),
        sa.Column("finished_at", _ts(), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
    )
    op.create_index("ix_runs_started_at", "runs", ["started_at"])

    op.create_table(
        "locks",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("acquired_at", _ts(), nullable=False),
        sa.PrimaryKeyConstraint("name", name="pk_locks"),
    )

    op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_templates"),
        sa.UniqueConstraint("version", name="uq_templates_version"),
    )


def downgrade() -> None:
    op.drop_table("templates")
    op.drop_table("locks")
    op.drop_index("ix_runs_started_at", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_email_lookups_created_at", table_name="email_lookups")
    op.drop_table("email_lookups")
    op.drop_index("ix_status_events_outreach_id", table_name="status_events")
    op.drop_table("status_events")
    op.drop_index("ix_outreach_updated_at", table_name="outreach")
    op.drop_index("ix_outreach_status", table_name="outreach")
    op.drop_table("outreach")
    op.drop_index("ix_founder_searches_search_id", table_name="founder_searches")
    op.drop_table("founder_searches")
    op.drop_table("founders")
    op.drop_table("searches")
