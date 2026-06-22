"""remove subsistema legado de memories

Revision ID: 0004
Revises: 0003

O subsistema `memories` (extração de fatos via job `extract_facts` e ferramentas
MCP de memória) foi removido. A tabela não era alimentada por nenhum fluxo ativo
do desenho atual (vault curado) e suas ferramentas MCP nunca foram registradas no
servidor. O grafo AGE preserva a propriedade genérica `source_memory` nas
entidades; apenas a tabela relacional é descartada aqui.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from pgvector.sqlalchemy import Vector

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

EMBED_DIM = 2000


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    op.drop_index("ix_memories_namespace", table_name="memories")
    op.drop_table("memories")


def downgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="fact"),
        sa.Column("source", sa.String(), nullable=False, server_default="conversation"),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("supersedes_id", UUID(as_uuid=True),
                  sa.ForeignKey("memories.id"), nullable=True),
        sa.Column("metadata", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_memories_namespace", "memories", ["namespace"])
    op.execute(
        "CREATE INDEX ix_memories_embedding_hnsw ON memories "
        "USING hnsw (embedding vector_cosine_ops)"
    )
