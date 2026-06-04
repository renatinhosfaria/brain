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
        namespace="trabalho", repo_path="notas/a.md", raw_content="oi mundo", content_hash="h1"
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
