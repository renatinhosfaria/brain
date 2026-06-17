import subprocess

import sqlalchemy as sa
from sqlalchemy import text


def test_alembic_upgrade_cria_tabelas(sync_dsn, async_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("BRAIN_AUTH_TOKEN", "t")
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
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    with engine.connect() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        ).scalars().all()
    assert {"documents", "chunks", "memories", "ingestion_jobs", "namespaces"} <= set(tables)
    expected = {
        "agent_clients",
        "agent_notes",
        "outbox_events",
        "note_links",
    }
    assert expected <= set(tables)
