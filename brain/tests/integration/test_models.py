import pytest_asyncio
from sqlalchemy import select

from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base, Chunk, Document, Namespace


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


async def test_inserir_documento_e_chunk_com_embedding(session):
    session.add(Namespace(name="trabalho"))
    doc = Document(
        namespace="trabalho",
        repo_path="notas/a.md",
        raw_content="oi mundo",
        content_hash="h1",
        meta={"origem": "teste"},
    )
    session.add(doc)
    await session.flush()
    session.add(
        Chunk(document_id=doc.id, ordinal=0, text="oi mundo", embedding=[0.1] * 2000, token_count=2)
    )
    await session.commit()

    chunks = (await session.execute(select(Chunk))).scalars().all()
    assert len(chunks) == 1
    assert len(chunks[0].embedding) == 2000

    documents = (await session.execute(select(Document))).scalars().all()
    assert documents[0].meta == {"origem": "teste"}


async def test_agent_inbox_models_insert(session):
    from brain.storage.models import AgentClient, AgentNote, NoteLink, OutboxEvent

    client = AgentClient(
        slug="chatgpt-web",
        name="ChatGPT Web",
        token_prefix="brain_client_chatgpt-web",
        token_hash="hash",
        token_encrypted="encrypted",
        permissions=["search", "get_note", "submit_agent_note"],
        meta={"host": "chatgpt"},
    )
    session.add(client)
    await session.flush()

    note = AgentNote(
        client_id=client.id,
        client_slug=client.slug,
        title="Resumo",
        repo_path="_agents/chatgpt-web/2026/06/17/resumo.md",
        status="pending",
        suggested_namespace="brain",
        meta={"model": "gpt"},
    )
    session.add(note)
    await session.flush()

    event = OutboxEvent(type="agent_note.created", payload={"note_id": str(note.id)})
    link = NoteLink(
        source_document_id=None,
        source_path="brain.md",
        target="MCP",
        raw="[[MCP]]",
    )
    session.add_all([event, link])
    await session.commit()

    clients = (await session.execute(select(AgentClient))).scalars().all()
    notes = (await session.execute(select(AgentNote))).scalars().all()
    events = (await session.execute(select(OutboxEvent))).scalars().all()
    links = (await session.execute(select(NoteLink))).scalars().all()

    assert clients[0].meta == {"host": "chatgpt"}
    assert notes[0].meta == {"model": "gpt"}
    assert events[0].status == "pending"
    assert links[0].status == "unresolved"
