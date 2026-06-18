"""adiciona backoff na fila de ingestao

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ingestion_jobs "
        "ADD COLUMN IF NOT EXISTS run_after TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ingestion_jobs DROP COLUMN IF EXISTS run_after")
