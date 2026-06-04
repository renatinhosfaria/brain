import pytest_asyncio

from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base


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
        session, namespace="t", repo_path="a.md", title="A",
        raw_content="oi", content_hash="h1", commit_sha=None,
    )
    await repo.replace_chunks(
        session, doc.id,
        [{"ordinal": 0, "text": "oi", "token_count": 1}],
        [_vec(0.1)],
    )
    await session.commit()
    # upsert idempotente: novo conteúdo substitui chunks
    doc2 = await repo.upsert_document(
        session, namespace="t", repo_path="a.md", title="A",
        raw_content="tchau", content_hash="h2", commit_sha=None,
    )
    assert doc2.id == doc.id
    await repo.replace_chunks(
        session, doc2.id, [{"ordinal": 0, "text": "tchau", "token_count": 1}], [_vec(0.2)]
    )
    await session.commit()
    docs = await repo.list_documents(session, "t")
    assert len(docs) == 1


async def test_busca_vetorial_retorna_mais_proximo(session):
    doc = await repo.upsert_document(
        session, namespace="t", repo_path="a.md", title=None,
        raw_content="x", content_hash="h", commit_sha=None,
    )
    await repo.replace_chunks(
        session, doc.id,
        [{"ordinal": 0, "text": "perto", "token_count": 1},
         {"ordinal": 1, "text": "longe", "token_count": 1}],
        [_vec(0.10), _vec(0.99)],
    )
    await session.commit()
    results = await repo.search_chunks(session, _vec(0.11), "t", limit=1)
    assert len(results) == 1
    assert results[0]["text"] == "perto"
    assert results[0]["source"] == "document"


async def test_memoria_crud_e_busca(session):
    mem = await repo.add_memory(session, namespace="p", content="gosta de café", embedding=_vec(0.5))
    await session.commit()
    assert (await repo.get_memory(session, mem.id)).content == "gosta de café"
    await repo.update_memory(session, mem.id, content="gosta de chá")
    await session.commit()
    assert (await repo.get_memory(session, mem.id)).content == "gosta de chá"
    res = await repo.search_memories(session, _vec(0.5), "p", limit=5)
    assert res[0]["source"] == "memory"
    assert await repo.delete_memory(session, mem.id) is True


async def test_namespace_idempotente(session):
    await repo.create_namespace(session, "t", "trabalho")
    await repo.create_namespace(session, "t", "trabalho")
    await session.commit()
    names = [n.name for n in await repo.list_namespaces(session)]
    assert names.count("t") == 1
