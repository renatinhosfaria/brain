import pytest_asyncio

from brain.graph import age
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
        await age.ensure_graph(s)
        # limpa o grafo entre execuções
        from sqlalchemy import text
        await age._prepare(s)
        await s.execute(text(
            "SELECT * FROM cypher('brain', $cy$ MATCH (n) DETACH DELETE n $cy$) AS (v agtype)"
        ))
        await s.commit()
        yield s
    await engine.dispose()


async def test_upsert_e_get_entity(session):
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho", {"papel": "dev"})
    got = await age.get_entity(session, "Renato", "trabalho")
    assert got["name"] == "Renato"
    assert got["type"] == "pessoa"
    assert got["props"] == {"papel": "dev"}


async def test_relacao_e_get_related(session):
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_relation(session, "Renato", "brain", "works_on", "trabalho")
    related = await age.get_related(session, "Renato", "trabalho", depth=1)
    assert {"name": "brain", "type": "projeto"} in related


async def test_search_entities(session):
    await age.upsert_entity(session, "Goiânia", "lugar", "pessoal")
    found = await age.search_entities(session, "goi", "pessoal")
    assert any(e["name"] == "Goiânia" for e in found)


async def test_delete_entity(session):
    await age.upsert_entity(session, "Temp", "conceito", "pessoal")
    await age.delete_entity(session, "Temp", "pessoal")
    assert await age.get_entity(session, "Temp", "pessoal") is None


async def test_merge_entities_move_relacoes(session):
    await age.upsert_entity(session, "TS", "conceito", "trabalho")
    await age.upsert_entity(session, "TypeScript", "conceito", "trabalho")
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_relation(session, "Renato", "TS", "likes", "trabalho")
    await age.merge_entities(session, ["TS"], "TypeScript", "trabalho")
    assert await age.get_entity(session, "TS", "trabalho") is None
    related = await age.get_related(session, "Renato", "trabalho")
    assert any(e["name"] == "TypeScript" for e in related)
