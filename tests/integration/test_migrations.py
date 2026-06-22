import subprocess

import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy import text


def test_alembic_upgrade_cria_tabelas(sync_dsn, async_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("BRAIN_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRAIN_CURATOR_TOKEN", "curator")
    monkeypatch.setenv("BRAIN_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEBHOOK_SECRET", "w")
    monkeypatch.setenv("REPO_URL", "https://x/y.git")

    # Garante banco limpo das tabelas (a imagem já tem extensões)
    engine = sa.create_engine(sync_dsn)
    with engine.begin() as conn:
        for t in [
            "note_links",
            "outbox_events",
            "agent_notes",
            "agent_clients",
            "ingestion_jobs",
            "memories",
            "chunks",
            "documents",
            "namespaces",
            "alembic_version",
        ]:
            conn.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))

    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    with engine.connect() as conn:
        tables = (
            conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
            .scalars()
            .all()
        )
        timestamp_nullability = {
            (table_name, column_name): is_nullable
            for table_name, column_name, is_nullable in conn.execute(
                text(
                    """
                    SELECT table_name, column_name, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    AND table_name IN (
                        'agent_clients',
                        'agent_notes',
                        'outbox_events',
                        'note_links'
                    )
                    AND column_name IN ('created_at', 'updated_at')
                    """
                )
            )
        }
        index_names = set(
            conn.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                    AND tablename IN ('agent_clients', 'agent_notes')
                    """
                )
            ).scalars()
        )
    assert {"documents", "chunks", "ingestion_jobs", "namespaces"} <= set(tables)
    assert "memories" not in set(tables)
    expected = {
        "agent_clients",
        "agent_notes",
        "outbox_events",
        "note_links",
    }
    assert expected <= set(tables)

    expected_not_nullable_timestamps = {
        ("agent_clients", "created_at"),
        ("agent_clients", "updated_at"),
        ("agent_notes", "created_at"),
        ("agent_notes", "updated_at"),
        ("outbox_events", "created_at"),
        ("outbox_events", "updated_at"),
        ("note_links", "created_at"),
    }
    for key in expected_not_nullable_timestamps:
        assert timestamp_nullability[key] == "NO"

    assert "ix_agent_clients_slug" not in index_names
    assert "ix_agent_clients_token_hash" not in index_names
    assert "ix_agent_notes_repo_path" not in index_names


def test_alembic_upgrade_de_legacy_0002_cria_inbox(sync_dsn, async_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("BRAIN_AUTH_TOKEN", "t")
    monkeypatch.setenv("BRAIN_CURATOR_TOKEN", "curator")
    monkeypatch.setenv("BRAIN_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEBHOOK_SECRET", "w")
    monkeypatch.setenv("REPO_URL", "https://x/y.git")

    engine = sa.create_engine(sync_dsn)
    with engine.begin() as conn:
        for t in [
            "note_links",
            "outbox_events",
            "agent_notes",
            "agent_clients",
            "ingestion_jobs",
            "memories",
            "chunks",
            "documents",
            "namespaces",
            "alembic_version",
        ]:
            conn.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))

    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "0001"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE ingestion_jobs "
                "ADD COLUMN IF NOT EXISTS run_after TIMESTAMP WITH TIME ZONE"
            )
        )
        conn.execute(text("UPDATE alembic_version SET version_num='0002'"))

    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    with engine.connect() as conn:
        tables = set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            ).scalars()
        )
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        has_run_after = conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name='ingestion_jobs'
                  AND column_name='run_after'
                """
            )
        ).scalar_one_or_none()

    assert {
        "agent_clients",
        "agent_notes",
        "outbox_events",
        "note_links",
    } <= tables
    assert has_run_after == 1
    assert version == "0004"
