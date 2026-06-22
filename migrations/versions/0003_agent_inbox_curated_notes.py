"""agent inbox e notas curadas

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "metadata",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_table(
        "agent_clients",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("token_prefix", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False, unique=True),
        sa.Column("token_encrypted", sa.Text(), nullable=False),
        sa.Column("permissions", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_clients_status", "agent_clients", ["status"])

    op.create_table(
        "agent_notes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "client_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_clients.id"),
            nullable=False,
        ),
        sa.Column("client_slug", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("repo_path", sa.String(), nullable=False, unique=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("suggested_namespace", sa.String(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("outcome", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_notes_client_slug", "agent_notes", ["client_slug"])
    op.create_index("ix_agent_notes_status", "agent_notes", ["status"])

    op.create_table(
        "outbox_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_outbox_events_type", "outbox_events", ["type"])
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])
    op.create_index("ix_outbox_events_run_after", "outbox_events", ["run_after"])

    op.create_table(
        "note_links",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("source_path", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("target_path", sa.String(), nullable=True),
        sa.Column("alias", sa.String(), nullable=True),
        sa.Column("anchor", sa.String(), nullable=True),
        sa.Column("raw", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="unresolved"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_note_links_source_path", "note_links", ["source_path"])
    op.create_index("ix_note_links_target", "note_links", ["target"])
    op.create_index("ix_note_links_target_path", "note_links", ["target_path"])
    op.create_index("ix_note_links_status", "note_links", ["status"])


def downgrade() -> None:
    op.drop_table("note_links")
    op.drop_table("outbox_events")
    op.drop_table("agent_notes")
    op.drop_table("agent_clients")
    op.drop_column("documents", "metadata")
