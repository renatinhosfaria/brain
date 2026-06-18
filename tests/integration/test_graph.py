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


async def test_get_relationship_paths_retorna_entidades_relacoes_direcao_e_depth(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_entity(session, "Vault", "conceito", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")
    await age.upsert_relation(session, "brain", "Vault", "stores", "curated")
    await session.commit()

    out = await age.get_relationship_paths(session, ["brain"], "curated", depth=2)

    assert {"name": "brain", "type": "projeto", "seed": "brain", "depth": 0} in out["entities"]
    assert {"name": "Hermes", "type": "agente", "seed": "brain", "depth": 1} in out["entities"]
    assert {"name": "Vault", "type": "conceito", "seed": "brain", "depth": 1} in out["entities"]
    assert {
        "from": "Hermes",
        "to": "brain",
        "type": "curates",
        "seed": "brain",
        "depth": 1,
    } in out["relationships"]
    assert {
        "from": "brain",
        "to": "Vault",
        "type": "stores",
        "seed": "brain",
        "depth": 1,
    } in out["relationships"]


async def test_get_relationship_paths_filtra_rel_types(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_entity(session, "Vault", "conceito", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")
    await age.upsert_relation(session, "brain", "Vault", "stores", "curated")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        ["brain"],
        "curated",
        depth=2,
        rel_types=["stores"],
    )

    assert {rel["type"] for rel in out["relationships"]} == {"stores"}
    assert {entity["name"] for entity in out["entities"]} == {"brain", "Vault"}


async def test_get_relationship_paths_deduplica_e_respeita_limit(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    for idx in range(3):
        name = f"Entidade {idx}"
        await age.upsert_entity(session, name, "conceito", "curated")
        await age.upsert_relation(session, "brain", name, "relates_to", "curated")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        ["brain", "brain"],
        "curated",
        depth=1,
        limit=2,
    )

    assert [(rel["to"], rel["depth"]) for rel in out["relationships"]] == [
        ("Entidade 0", 1),
        ("Entidade 1", 1),
    ]
    assert len(out["relationships"]) == 2
    assert len(out["entities"]) == 3
    assert {e["name"] for e in out["entities"]} == {"brain", "Entidade 0", "Entidade 1"}


async def test_get_relationship_paths_limita_entidades_pelas_relacoes_finais(session):
    await age.upsert_entity(session, "A", "projeto", "curated")
    await age.upsert_entity(session, "B", "projeto", "curated")
    await age.upsert_entity(session, "X", "conceito", "curated")
    await age.upsert_relation(session, "A", "X", "mentions", "curated")
    await age.upsert_relation(session, "B", "X", "mentions", "curated")
    await session.commit()

    out = await age.get_relationship_paths(session, ["B", "A"], "curated", depth=1, limit=1)

    assert len(out["relationships"]) == 1
    surviving_rel = out["relationships"][0]
    entity_x = [e for e in out["entities"] if e["name"] == "X"][0]
    assert entity_x["seed"] == surviving_rel["seed"]
    entity_names = {entity["name"] for entity in out["entities"]}
    assert entity_names == {surviving_rel["from"], surviving_rel["to"]}


async def test_get_relationship_paths_deduplica_entidade_por_nome_no_namespace(session):
    await age.upsert_entity(session, "A", "projeto", "curated")
    await age.upsert_entity(session, "B", "projeto", "curated")
    await age.upsert_entity(session, "X", "conceito", "curated")
    await age.upsert_relation(session, "A", "X", "mentions", "curated")
    await age.upsert_relation(session, "B", "X", "mentions", "curated")
    await session.commit()

    out = await age.get_relationship_paths(session, ["A", "B"], "curated", depth=1)

    xs = [e for e in out["entities"] if e["name"] == "X"]
    assert len(xs) == 1
    assert xs[0]["depth"] == 1
    assert xs[0]["seed"] == "A"


async def test_get_relationship_paths_rel_types_descarta_path_com_aresta_fora(session):
    await age.upsert_entity(session, "seed", "projeto", "curated")
    await age.upsert_entity(session, "intermediaria", "conceito", "curated")
    await age.upsert_entity(session, "alvo", "conceito", "curated")
    await age.upsert_entity(session, "destino", "conceito", "curated")

    await age.upsert_relation(session, "seed", "intermediaria", "drop", "curated")
    await age.upsert_relation(session, "intermediaria", "alvo", "keep", "curated")
    await age.upsert_relation(session, "seed", "destino", "keep", "curated")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        ["seed"],
        "curated",
        depth=2,
        rel_types=["keep"],
    )

    assert {rel["type"] for rel in out["relationships"]} == {"keep"}
    assert "intermediaria" not in {entity["name"] for entity in out["entities"]}
    assert "alvo" not in {entity["name"] for entity in out["entities"]}
    assert "destino" in {entity["name"] for entity in out["entities"]}


async def test_get_relationship_paths_deep_depth_de_nodes(session):
    await age.upsert_entity(session, "seed", "projeto", "curated")
    await age.upsert_entity(session, "A", "conceito", "curated")
    await age.upsert_entity(session, "B", "conceito", "curated")
    await age.upsert_relation(session, "seed", "A", "next", "curated")
    await age.upsert_relation(session, "A", "B", "next", "curated")
    await session.commit()

    out = await age.get_relationship_paths(session, ["seed"], "curated", depth=2)

    assert {"name": "seed", "type": "projeto", "seed": "seed", "depth": 0} in out["entities"]
    assert {"name": "A", "type": "conceito", "seed": "seed", "depth": 1} in out["entities"]
    assert {"name": "B", "type": "conceito", "seed": "seed", "depth": 2} in out["entities"]


async def test_get_relationship_paths_limit_com_rel_types_aplica_filtro_antes_do_limit(session):
    await age.upsert_entity(session, "seed", "projeto", "curated")
    await age.upsert_entity(session, "Alpha", "conceito", "curated")
    await age.upsert_entity(session, "Beta", "conceito", "curated")
    await age.upsert_relation(session, "seed", "Alpha", "drop", "curated")
    await age.upsert_relation(session, "seed", "Beta", "keep", "curated")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        ["seed"],
        "curated",
        depth=1,
        rel_types=["keep"],
        limit=1,
    )

    assert out["relationships"] == [
        {"from": "seed", "to": "Beta", "type": "keep", "seed": "seed", "depth": 1}
    ]
