import datetime as dt
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBED_DIM = 2000


class Base(DeclarativeBase):
    pass


class Namespace(Base):
    __tablename__ = "namespaces"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    namespace: Mapped[str] = mapped_column(String, index=True)
    repo_path: Mapped[str] = mapped_column(String, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String, index=True)
    commit_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    token_count: Mapped[int] = mapped_column(Integer)

    document: Mapped["Document"] = relationship(back_populates="chunks")


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    namespace: Mapped[str] = mapped_column(String, index=True)
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String, default="fact")
    source: Mapped[str] = mapped_column(String, default="conversation")
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id"), nullable=True
    )
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
