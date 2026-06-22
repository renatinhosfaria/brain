import asyncio
import datetime as dt
import uuid

import pytest_asyncio
from sqlalchemy import select

from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base, NoteLink, OutboxEvent


@pytest_asyncio.fixture
async def session(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


def _vec(seed: float) -> list[float]:
    # Direção que varia com `seed`: a distância de cosseno só considera a
    # direção do vetor, então um vetor constante ([seed]*2000) seria paralelo a
    # qualquer outro (distância 0). Aqui aproximar `seed` aproxima o ângulo.
    return [1.0, seed] + [0.0] * 1998


async def test_upsert_documento_e_replace_chunks(session):
    doc = await repo.upsert_document(
        session,
        namespace="t",
        repo_path="a.md",
        title="A",
        raw_content="oi",
        content_hash="h1",
        commit_sha=None,
    )
    await repo.replace_chunks(
        session,
        doc.id,
        [{"ordinal": 0, "text": "oi", "token_count": 1}],
        [_vec(0.1)],
    )
    await session.commit()
    # upsert idempotente: novo conteúdo substitui chunks
    doc2 = await repo.upsert_document(
        session,
        namespace="t",
        repo_path="a.md",
        title="A",
        raw_content="tchau",
        content_hash="h2",
        commit_sha=None,
    )
    assert doc2.id == doc.id
    await repo.replace_chunks(
        session, doc2.id, [{"ordinal": 0, "text": "tchau", "token_count": 1}], [_vec(0.2)]
    )
    await session.commit()
    docs = await repo.list_documents(session, "t")
    assert len(docs) == 1


async def test_upsert_document_persiste_metadata_sem_quebrar_chamadas_existentes(session):
    doc = await repo.upsert_document(
        session,
        namespace="t",
        repo_path="meta.md",
        title="Meta",
        raw_content="oi",
        content_hash="h1",
        commit_sha=None,
        meta={"tags": ["brain"], "source": "hermes"},
    )
    await session.commit()

    fetched = await repo.get_document(session, repo_path="meta.md")
    assert fetched.id == doc.id
    assert fetched.meta == {"tags": ["brain"], "source": "hermes"}

    updated = await repo.upsert_document(
        session,
        namespace="t",
        repo_path="meta.md",
        title="Meta",
        raw_content="novo",
        content_hash="h2",
        commit_sha="abc123",
    )
    await session.commit()

    assert updated.id == doc.id
    assert updated.meta == {"tags": ["brain"], "source": "hermes"}


async def test_busca_vetorial_retorna_mais_proximo(session):
    doc = await repo.upsert_document(
        session,
        namespace="t",
        repo_path="a.md",
        title=None,
        raw_content="x",
        content_hash="h",
        commit_sha=None,
    )
    await repo.replace_chunks(
        session,
        doc.id,
        [
            {"ordinal": 0, "text": "perto", "token_count": 1},
            {"ordinal": 1, "text": "longe", "token_count": 1},
        ],
        [_vec(0.10), _vec(0.99)],
    )
    await session.commit()
    results = await repo.search_chunks(session, _vec(0.11), "t", limit=1)
    assert len(results) == 1
    assert results[0]["text"] == "perto"
    assert results[0]["source"] == "document"


async def test_namespace_idempotente(session):
    await repo.create_namespace(session, "t", "trabalho")
    await repo.create_namespace(session, "t", "trabalho")
    await session.commit()
    names = [n.name for n in await repo.list_namespaces(session)]
    assert names.count("t") == 1


async def test_agent_client_create_get_list_disable_sao_idempotentes_por_slug(session):
    client = await repo.create_agent_client(
        session,
        slug="chatgpt-web",
        name="ChatGPT Web",
        description="web client",
        token_prefix="brain_client_chatgpt-web",
        token_hash="hash-v1",
        token_encrypted="encrypted-v1",
        permissions=["search", "get_note", "submit_agent_note"],
        meta={"host": "chatgpt"},
    )
    duplicate = await repo.create_agent_client(
        session,
        slug="chatgpt-web",
        name="Nome ignorado",
        description="descricao ignorada",
        token_prefix="brain_client_chatgpt-web-v2",
        token_hash="hash-v2",
        token_encrypted="encrypted-v2",
        permissions=["search"],
        meta={"host": "outro"},
    )
    await session.commit()

    assert duplicate.id == client.id

    fetched = await repo.get_agent_client(session, slug="chatgpt-web")
    assert fetched.id == client.id
    assert fetched.name == "ChatGPT Web"
    assert fetched.description == "web client"
    assert fetched.token_hash == "hash-v1"
    assert fetched.meta == {"host": "chatgpt"}
    assert await repo.get_agent_client(session, slug="ausente") is None

    clients = await repo.list_agent_clients(session)
    assert [c.slug for c in clients] == ["chatgpt-web"]

    disabled = await repo.disable_agent_client(session, "chatgpt-web")
    await session.commit()
    assert disabled.status == "disabled"
    assert (await repo.get_agent_client(session, slug="chatgpt-web")).status == "disabled"


async def test_agent_client_lookup_token_rotation_e_last_seen(session):
    client = await repo.create_agent_client(
        session,
        slug="codex",
        name="Codex",
        description=None,
        token_prefix="brain_client_codex",
        token_hash="hash-v1",
        token_encrypted="encrypted-v1",
        permissions=["submit_agent_note"],
        meta=None,
    )
    await session.commit()

    assert (await repo.get_agent_client_by_token_hash(session, "hash-v1")).id == client.id

    rotated = await repo.update_agent_client_token(
        session,
        slug="codex",
        token_prefix="brain_client_codex_rotated",
        token_hash="hash-v2",
        token_encrypted="encrypted-v2",
    )
    touched = await repo.touch_agent_client_seen(session, "codex")
    await session.commit()

    assert rotated.id == client.id
    assert rotated.token_prefix == "brain_client_codex_rotated"
    assert rotated.token_hash == "hash-v2"
    assert rotated.token_encrypted == "encrypted-v2"
    assert touched.last_seen_at is not None
    assert await repo.get_agent_client_by_token_hash(session, "hash-v1") is None
    assert (await repo.get_agent_client_by_token_hash(session, "hash-v2")).slug == "codex"


async def test_agent_client_create_retorna_existente_em_corrida_por_slug(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)

    async with factory() as first, factory() as second:
        created = await repo.create_agent_client(
            first,
            slug="chatgpt-web",
            name="ChatGPT Web",
            description="web client",
            token_prefix="brain_client_chatgpt-web",
            token_hash="hash-v1",
            token_encrypted="encrypted-v1",
            permissions=["search"],
            meta={"host": "chatgpt"},
        )

        async def create_duplicate():
            duplicate = await repo.create_agent_client(
                second,
                slug="chatgpt-web",
                name="Nome ignorado",
                description="descricao ignorada",
                token_prefix="brain_client_chatgpt-web-v2",
                token_hash="hash-v2",
                token_encrypted="encrypted-v2",
                permissions=["submit_agent_note"],
                meta={"host": "outro"},
            )
            await second.commit()
            return duplicate

        duplicate_task = asyncio.create_task(create_duplicate())
        await asyncio.sleep(0.2)
        await first.commit()
        duplicate = await asyncio.wait_for(duplicate_task, timeout=5)

    async with factory() as verify:
        fetched = await repo.get_agent_client(verify, slug="chatgpt-web")
        assert duplicate.id == created.id
        assert fetched.id == created.id
        assert fetched.name == "ChatGPT Web"
        assert fetched.description == "web client"
        assert fetched.token_hash == "hash-v1"
        assert fetched.token_encrypted == "encrypted-v1"
        assert fetched.permissions == ["search"]
        assert fetched.meta == {"host": "chatgpt"}
        assert await repo.get_agent_client_by_token_hash(verify, "hash-v2") is None

    await engine.dispose()


async def test_agent_note_create_get_list_e_update_status(session):
    client = await repo.create_agent_client(
        session,
        slug="chatgpt-web",
        name="ChatGPT Web",
        description=None,
        token_prefix="brain_client_chatgpt-web",
        token_hash="hash",
        token_encrypted="encrypted",
        permissions=[],
        meta=None,
    )
    other_client = await repo.create_agent_client(
        session,
        slug="codex",
        name="Codex",
        description=None,
        token_prefix="brain_client_codex",
        token_hash="hash-codex",
        token_encrypted="encrypted-codex",
        permissions=[],
        meta=None,
    )

    older = await repo.create_agent_note(
        session,
        client_id=client.id,
        client_slug=client.slug,
        title="Resumo antigo",
        repo_path="_agents/chatgpt-web/2026/06/17/antigo.md",
        suggested_namespace="brain",
        meta={"model": "gpt"},
    )
    newer = await repo.create_agent_note(
        session,
        client_id=client.id,
        client_slug=client.slug,
        title="Resumo novo",
        repo_path="_agents/chatgpt-web/2026/06/17/novo.md",
        suggested_namespace=None,
        meta=None,
    )
    other = await repo.create_agent_note(
        session,
        client_id=other_client.id,
        client_slug=other_client.slug,
        title="Outro",
        repo_path="_agents/codex/2026/06/17/outro.md",
        suggested_namespace=None,
        meta=None,
    )
    older.created_at = dt.datetime(2026, 6, 17, 10, tzinfo=dt.UTC)
    newer.created_at = dt.datetime(2026, 6, 17, 11, tzinfo=dt.UTC)
    other.created_at = dt.datetime(2026, 6, 17, 12, tzinfo=dt.UTC)
    await session.commit()

    assert (await repo.get_agent_note(session, older.id)).repo_path == older.repo_path
    assert await repo.get_agent_note(session, uuid.uuid4()) is None

    listed = await repo.list_agent_notes(session, limit=2)
    assert [n.id for n in listed] == [other.id, newer.id]

    client_notes = await repo.list_agent_notes(session, client_slug="chatgpt-web")
    assert [n.id for n in client_notes] == [newer.id, older.id]

    updated = await repo.update_agent_note_status(
        session,
        older.id,
        "curated",
        outcome={"created_notes": ["projetos/brain.md"]},
        error=None,
    )
    await session.commit()

    assert updated.status == "curated"
    assert updated.outcome == {"created_notes": ["projetos/brain.md"]}
    assert updated.error is None
    assert [n.id for n in await repo.list_agent_notes(session, status="curated")] == [older.id]


async def test_outbox_event_create_claim_e_mark(session):
    now = dt.datetime(2026, 6, 17, 12, tzinfo=dt.UTC)
    event = await repo.create_outbox_event(
        session,
        type="agent_note.created",
        payload={"note_id": "note-1"},
    )
    await session.commit()

    claimed = await repo.claim_next_outbox_event(session, now, worker_id="worker-1")
    assert claimed.id == event.id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert claimed.locked_by == "worker-1"
    assert claimed.locked_at == now
    claim = repo.outbox_claim_token(claimed)

    run_after = now + dt.timedelta(minutes=5)
    retrying = await repo.mark_outbox_retrying(
        session,
        claimed.id,
        error="Hermes indisponivel",
        run_after=run_after,
        claim=claim,
    )
    assert retrying.status == "retrying"
    assert retrying.last_error == "Hermes indisponivel"
    assert retrying.run_after == run_after
    assert retrying.locked_by is None
    assert retrying.locked_at is None
    assert await repo.claim_next_outbox_event(session, now, worker_id="worker-1") is None

    claimed_again = await repo.claim_next_outbox_event(
        session,
        run_after,
        worker_id="worker-2",
    )
    assert claimed_again.id == event.id
    assert claimed_again.status == "running"
    assert claimed_again.attempts == 2
    assert claimed_again.locked_by == "worker-2"
    claim_again = repo.outbox_claim_token(claimed_again)

    delivered = await repo.mark_outbox_delivered(session, event.id, claim=claim_again)
    assert delivered.status == "delivered"
    assert delivered.locked_by is None
    assert delivered.locked_at is None

    failed_event = await repo.create_outbox_event(
        session,
        type="agent_note.failed",
        payload={"note_id": "note-2"},
    )
    failed_claimed = await repo.claim_next_outbox_event(session, now, worker_id="worker-1")
    assert failed_claimed.id == failed_event.id
    failed = await repo.mark_outbox_failed(
        session,
        failed_event.id,
        error="erro permanente",
        claim=repo.outbox_claim_token(failed_claimed),
    )
    await session.commit()

    assert failed.status == "failed"
    assert failed.last_error == "erro permanente"
    assert failed.locked_by is None
    assert failed.locked_at is None


async def test_outbox_event_reclama_running_stale(session):
    now = dt.datetime(2026, 6, 17, 12, tzinfo=dt.UTC)
    event = await repo.create_outbox_event(
        session,
        type="agent_note.created",
        payload={"note_id": "note-1"},
    )
    await session.commit()

    claimed = await repo.claim_next_outbox_event(session, now, worker_id="worker-1")
    await session.commit()
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert claimed.locked_at == now
    assert claimed.locked_by == "worker-1"

    assert (
        await repo.claim_next_outbox_event(
            session,
            now + dt.timedelta(minutes=10),
            worker_id="worker-2",
        )
        is None
    )
    assert (
        await repo.claim_next_outbox_event(
            session,
            now + dt.timedelta(minutes=10),
            worker_id="worker-2",
            stale_before=now - dt.timedelta(seconds=1),
        )
        is None
    )

    reclaimed = await repo.claim_next_outbox_event(
        session,
        now + dt.timedelta(minutes=10),
        worker_id="worker-2",
        stale_before=now,
    )
    await session.commit()

    assert reclaimed.id == event.id
    assert reclaimed.status == "running"
    assert reclaimed.attempts == 2
    assert reclaimed.locked_at == now + dt.timedelta(minutes=10)
    assert reclaimed.locked_by == "worker-2"


async def test_outbox_mark_ignora_claim_stale_apos_reclaim_e_delivered(session):
    now = dt.datetime(2026, 6, 17, 12, tzinfo=dt.UTC)
    event = await repo.create_outbox_event(
        session,
        type="agent_note.created",
        payload={"note_id": "note-1"},
    )
    await session.commit()

    worker_a = await repo.claim_next_outbox_event(session, now, worker_id="worker-a")
    worker_a_claim = repo.outbox_claim_token(worker_a)
    await session.commit()

    worker_b_now = now + dt.timedelta(minutes=10)
    worker_b = await repo.claim_next_outbox_event(
        session,
        worker_b_now,
        worker_id="worker-b",
        stale_before=now,
    )
    worker_b_claim = repo.outbox_claim_token(worker_b)
    delivered = await repo.mark_outbox_delivered(
        session,
        event.id,
        claim=worker_b_claim,
    )
    await session.commit()

    assert delivered.status == "delivered"
    assert delivered.attempts == 2

    assert (
        await repo.mark_outbox_retrying(
            session,
            event.id,
            error="worker-a retry atrasado",
            run_after=worker_b_now + dt.timedelta(minutes=5),
            claim=worker_a_claim,
        )
        is None
    )
    assert (
        await repo.mark_outbox_failed(
            session,
            event.id,
            error="worker-a failed atrasado",
            claim=worker_a_claim,
        )
        is None
    )
    assert (
        await repo.mark_outbox_delivered(
            session,
            event.id,
            claim=worker_a_claim,
        )
        is None
    )
    await session.commit()

    stored = (
        await session.execute(select(OutboxEvent).where(OutboxEvent.id == event.id))
    ).scalar_one()
    assert stored.status == "delivered"
    assert stored.attempts == 2
    assert stored.last_error is None
    assert stored.run_after is None
    assert stored.locked_at is None
    assert stored.locked_by is None


async def test_note_link_replace_list_unresolved_e_resolve(session):
    doc = await repo.upsert_document(
        session,
        namespace="brain",
        repo_path="projetos/brain.md",
        title="Brain",
        raw_content="# Brain",
        content_hash="hash",
        commit_sha=None,
    )

    links = await repo.replace_note_links(
        session,
        source_document_id=doc.id,
        source_path="projetos/brain.md",
        links=[
            {"target": "MCP", "alias": "protocolo", "anchor": None, "raw": "[[MCP|protocolo]]"},
            {
                "target": "Hermes",
                "target_path": "agentes/hermes.md",
                "raw": "[[Hermes]]",
                "status": "resolved",
            },
        ],
    )
    await session.commit()

    assert len(links) == 2
    unresolved = await repo.list_unresolved_links(session)
    assert [link.target for link in unresolved] == ["MCP"]

    resolved = await repo.resolve_note_link(
        session,
        unresolved[0].id,
        target_path="protocolos/mcp.md",
    )
    assert resolved.status == "resolved"
    assert resolved.target_path == "protocolos/mcp.md"

    replacement = await repo.replace_note_links(
        session,
        source_document_id=doc.id,
        source_path="projetos/brain.md",
        links=[{"target": "Brain", "raw": "[[Brain]]"}],
    )
    await session.commit()

    stored = (await session.execute(select(NoteLink))).scalars().all()
    assert [link.id for link in stored] == [replacement[0].id]
    assert [link.target for link in await repo.list_unresolved_links(session)] == ["Brain"]
