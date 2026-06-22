import datetime as dt

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
        await s.execute(
            text("SELECT * FROM cypher('brain', $cy$ MATCH (n) DETACH DELETE n $cy$) AS (v agtype)")
        )
        await s.commit()
        yield s
    await engine.dispose()


async def test_upsert_e_get_entity(session):
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho", {"papel": "dev"})
    got = await age.get_entity(session, "Renato", "trabalho")
    assert got["name"] == "Renato"
    assert got["type"] == "pessoa"
    assert got["props"]["papel"] == "dev"


async def test_upsert_define_valid_at_e_mantem_invalid_at_nulo(session):
    await age.upsert_entity(session, "Alfa", "conceito", "t", {"source_doc": "x.md"})
    got = await age.get_entity(session, "Alfa", "t")
    assert got["valid_at"] is not None
    assert got["invalid_at"] is None


async def test_temporalidade_invalidacao_e_as_of(session):
    now = dt.datetime.now(dt.UTC)
    between = (now + dt.timedelta(hours=1)).isoformat()
    later = (now + dt.timedelta(days=1)).isoformat()
    after = (now + dt.timedelta(days=2)).isoformat()

    await age.upsert_entity(session, "Alfa", "conceito", "t", {"source_doc": "x.md"})
    await age.upsert_entity(session, "Beta", "conceito", "t", {"source_doc": "x.md"})
    await age.upsert_relation(session, "Alfa", "Beta", "rel", "t")

    # Invalida as entidades do doc x.md a partir de `later`.
    await age.invalidate_entities_by_source_doc(session, "x.md", "t", at=later)
    alfa = await age.get_entity(session, "Alfa", "t")
    assert alfa is not None  # preservada (histórico)
    assert alfa["invalid_at"] == later

    seeds = [{"name": "Alfa", "namespace": "t"}]

    # Agora: Alfa inválida -> não alcança Beta.
    g_now = await age.get_relationship_paths(session, seeds, "t", depth=1)
    assert all(e["name"] != "Beta" for e in g_now["entities"])

    # as_of entre valid_at e invalid_at: válida -> alcança Beta.
    g_past = await age.get_relationship_paths(session, seeds, "t", depth=1, as_of=between)
    assert any(e["name"] == "Beta" for e in g_past["entities"])

    # as_of depois de invalid_at: inválida de novo.
    g_after = await age.get_relationship_paths(session, seeds, "t", depth=1, as_of=after)
    assert all(e["name"] != "Beta" for e in g_after["entities"])


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


async def test_search_entities_sem_namespace_retorna_todos_com_namespace(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await session.commit()

    found = await age.search_entities(session, "brain", None)

    assert found == [
        {"name": "brain", "type": "projeto", "namespace": "curated"},
        {"name": "brain", "type": "projeto", "namespace": "trabalho"},
    ]


async def test_search_entities_sem_namespace_respeita_limit_global(session):
    await age.upsert_entity(session, "Brain A", "projeto", "curated")
    await age.upsert_entity(session, "Brain B", "projeto", "trabalho")
    await age.upsert_entity(session, "Brain C", "projeto", "pessoal")
    await session.commit()

    found = await age.search_entities(session, "Brain", None, limit=2)

    assert len(found) == 2
    assert all("namespace" in entity for entity in found)


async def test_delete_entity(session):
    await age.upsert_entity(session, "Temp", "conceito", "pessoal")
    await age.delete_entity(session, "Temp", "pessoal")
    assert await age.get_entity(session, "Temp", "pessoal") is None


async def test_delete_entities_by_source_doc_preserva_sources_excluidos(session):
    await age.upsert_entity(
        session,
        "Curada",
        "preferencia",
        "curated",
        {"source": "curated_note", "source_doc": "preferencias/x.md"},
    )
    await age.upsert_entity(
        session,
        "Extraida",
        "conceito",
        "curated",
        {"source_doc": "preferencias/x.md"},
    )
    await age.upsert_entity(
        session,
        "Outro Doc",
        "conceito",
        "curated",
        {"source": "curated_note", "source_doc": "preferencias/outro.md"},
    )

    await age.delete_entities_by_source_doc(
        session,
        "preferencias/x.md",
        "curated",
        exclude_sources={"curated_note"},
    )

    assert await age.get_entity(session, "Curada", "curated") is not None
    assert await age.get_entity(session, "Extraida", "curated") is None
    assert await age.get_entity(session, "Outro Doc", "curated") is not None


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

    assert {
        "name": "brain",
        "type": "projeto",
        "namespace": "curated",
        "seed": "brain",
        "depth": 0,
    } in out["entities"]
    assert {
        "name": "Hermes",
        "type": "agente",
        "namespace": "curated",
        "seed": "brain",
        "depth": 1,
    } in out["entities"]
    assert {
        "name": "Vault",
        "type": "conceito",
        "namespace": "curated",
        "seed": "brain",
        "depth": 1,
    } in out["entities"]
    assert {
        "from": "Hermes",
        "to": "brain",
        "type": "curates",
        "namespace": "curated",
        "seed": "brain",
        "depth": 1,
    } in out["relationships"]
    assert {
        "from": "brain",
        "to": "Vault",
        "type": "stores",
        "namespace": "curated",
        "seed": "brain",
        "depth": 1,
    } in out["relationships"]


async def test_get_relationship_paths_global_retorna_lista_unica_com_namespace(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")

    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_relation(session, "Renato", "brain", "owns", "trabalho")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        [
            {"name": "brain", "namespace": "curated"},
            {"name": "brain", "namespace": "trabalho"},
        ],
        None,
        depth=1,
    )

    assert out["relationships"] == [
        {
            "from": "Hermes",
            "to": "brain",
            "type": "curates",
            "namespace": "curated",
            "seed": "brain",
            "depth": 1,
        },
        {
            "from": "Renato",
            "to": "brain",
            "type": "owns",
            "namespace": "trabalho",
            "seed": "brain",
            "depth": 1,
        },
    ]
    assert {(entity["name"], entity["namespace"]) for entity in out["entities"]} == {
        ("brain", "curated"),
        ("Hermes", "curated"),
        ("brain", "trabalho"),
        ("Renato", "trabalho"),
    }


async def test_get_relationship_paths_namespace_expresso_descarta_seed_de_outro_namespace(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_relation(session, "Hermes", "brain", "curates", "curated")

    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_relation(session, "Renato", "brain", "owns", "trabalho")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        [
            {"name": "brain", "namespace": "curated"},
            {"name": "brain", "namespace": "trabalho"},
        ],
        "curated",
        depth=1,
    )

    assert out["relationships"] == [
        {
            "from": "Hermes",
            "to": "brain",
            "type": "curates",
            "namespace": "curated",
            "seed": "brain",
            "depth": 1,
        }
    ]
    assert {entity["namespace"] for entity in out["entities"]} == {"curated"}


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


async def test_get_relationship_paths_nao_atravessa_relacao_cross_namespace(session):
    from sqlalchemy import text

    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Vault", "conceito", "curated")
    await age.upsert_entity(session, "Tenant Secret", "conceito", "tenant-b")
    await age.upsert_entity(session, "Public Leak", "conceito", "curated")
    await age.upsert_relation(session, "brain", "Vault", "stores", "curated")
    await age._prepare(session)
    leaked_to = (
        await session.execute(
            text(
                f"SELECT * FROM cypher('brain', $cy$ "
                f"MATCH (a:Entity {{name: {age._lit('brain')}, "
                f"namespace: {age._lit('curated')}}}), "
                f"(b:Entity {{name: {age._lit('Tenant Secret')}, "
                f"namespace: {age._lit('tenant-b')}}}) "
                f"MERGE (a)-[r:REL {{type: {age._lit('leaks_to')}}}]->(b) "
                f"RETURN r.type $cy$) AS (type agtype)"
            )
        )
    ).first()
    leaked_back = (
        await session.execute(
            text(
                f"SELECT * FROM cypher('brain', $cy$ "
                f"MATCH (a:Entity {{name: {age._lit('Tenant Secret')}, "
                f"namespace: {age._lit('tenant-b')}}}), "
                f"(b:Entity {{name: {age._lit('Public Leak')}, namespace: {age._lit('curated')}}}) "
                f"MERGE (a)-[r:REL {{type: {age._lit('leaks_back')}}}]->(b) "
                f"RETURN r.type $cy$) AS (type agtype)"
            )
        )
    ).first()
    assert leaked_to is not None
    assert leaked_back is not None
    await session.commit()

    out = await age.get_relationship_paths(session, ["brain"], "curated", depth=2)

    assert {"Tenant Secret", "Public Leak"}.isdisjoint(
        {entity["name"] for entity in out["entities"]}
    )
    assert {"leaks_to", "leaks_back"}.isdisjoint({rel["type"] for rel in out["relationships"]})
    assert out["relationships"] == [
        {
            "from": "brain",
            "to": "Vault",
            "type": "stores",
            "namespace": "curated",
            "seed": "brain",
            "depth": 1,
        }
    ]


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

    assert {
        "name": "seed",
        "type": "projeto",
        "namespace": "curated",
        "seed": "seed",
        "depth": 0,
    } in out["entities"]
    assert {
        "name": "A",
        "type": "conceito",
        "namespace": "curated",
        "seed": "seed",
        "depth": 1,
    } in out["entities"]
    assert {
        "name": "B",
        "type": "conceito",
        "namespace": "curated",
        "seed": "seed",
        "depth": 2,
    } in out["entities"]


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
        {
            "from": "seed",
            "to": "Beta",
            "type": "keep",
            "namespace": "curated",
            "seed": "seed",
            "depth": 1,
        }
    ]


async def test_get_relationship_paths_ordena_empate_de_tipo_de_relacao(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Hermes", "agente", "curated")
    await age.upsert_relation(session, "brain", "Hermes", "zeta", "curated")
    await age.upsert_relation(session, "brain", "Hermes", "alpha", "curated")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        ["brain"],
        "curated",
        depth=1,
        rel_types=["alpha", "zeta"],
        limit=1,
    )

    assert out["relationships"] == [
        {
            "from": "brain",
            "to": "Hermes",
            "type": "alpha",
            "namespace": "curated",
            "seed": "brain",
            "depth": 1,
        }
    ]


async def test_get_relationship_paths_ordena_empate_por_intermediario(session):
    await age.upsert_entity(session, "brain", "projeto", "curated")
    await age.upsert_entity(session, "Alpha", "conceito", "curated")
    await age.upsert_entity(session, "Beta", "conceito", "curated")
    await age.upsert_entity(session, "Omega", "conceito", "curated")
    await age.upsert_relation(session, "brain", "Alpha", "rel", "curated")
    await age.upsert_relation(session, "Alpha", "Omega", "rel", "curated")
    await age.upsert_relation(session, "brain", "Beta", "rel", "curated")
    await age.upsert_relation(session, "Beta", "Omega", "rel", "curated")
    await session.commit()

    out = await age.get_relationship_paths(
        session,
        ["brain"],
        "curated",
        depth=2,
        rel_types=["rel"],
        limit=3,
    )

    assert out["relationships"] == [
        {
            "from": "brain",
            "to": "Alpha",
            "type": "rel",
            "namespace": "curated",
            "seed": "brain",
            "depth": 1,
        },
        {
            "from": "brain",
            "to": "Beta",
            "type": "rel",
            "namespace": "curated",
            "seed": "brain",
            "depth": 1,
        },
        {
            "from": "Alpha",
            "to": "Omega",
            "type": "rel",
            "namespace": "curated",
            "seed": "brain",
            "depth": 2,
        },
    ]


async def test_find_entity_by_source_doc_and_update_identity_preserves_relation(session):
    await age.upsert_entity(
        session,
        "Nome Antigo",
        "conceito",
        "curated",
        {"source_doc": "preferencias/x.md", "aliases": ["antigo"]},
    )
    await age.upsert_entity(session, "Vizinho", "conceito", "curated")
    await age.upsert_relation(session, "Nome Antigo", "Vizinho", "relates_to", "curated")

    found = await age.find_entity_by_source_doc(
        session,
        namespace="curated",
        source_doc="preferencias/x.md",
    )
    assert found["name"] == "Nome Antigo"

    await age.update_entity_identity(
        session,
        entity=found,
        namespace="curated",
        name="Nome Novo",
        type="preferencia",
        props={"source_doc": "preferencias/x.md", "aliases": ["novo"]},
        commit=False,
    )
    await session.commit()

    assert await age.get_entity(session, "Nome Antigo", "curated") is None
    got = await age.get_entity(session, "Nome Novo", "curated")
    assert got["type"] == "preferencia"
    related = await age.get_related(session, "Nome Novo", "curated")
    assert {"name": "Vizinho", "type": "conceito"} in related


async def test_find_entity_by_source_doc_prefers_curated_note_over_llm_entity(session):
    await age.upsert_entity(
        session,
        "Z Curada",
        "preferencia",
        "curated",
        {
            "source": "curated_note",
            "source_doc": "preferencias/mesma.md",
            "document_id": "doc-curated",
        },
    )
    await age.upsert_entity(
        session,
        "A Extraida",
        "conceito",
        "curated",
        {"source_doc": "preferencias/mesma.md"},
    )

    found = await age.find_entity_by_source_doc(
        session,
        namespace="curated",
        source_doc="preferencias/mesma.md",
    )

    assert found["name"] == "Z Curada"


async def test_find_entity_by_source_doc_prefers_exact_document_id(session):
    await age.upsert_entity(
        session,
        "Documento A",
        "preferencia",
        "curated",
        {
            "source": "curated_note",
            "source_doc": "preferencias/mesma.md",
            "document_id": "doc-a",
        },
    )
    await age.upsert_entity(
        session,
        "Documento Z",
        "preferencia",
        "curated",
        {
            "source": "curated_note",
            "source_doc": "preferencias/mesma.md",
            "document_id": "doc-z",
        },
    )

    found = await age.find_entity_by_source_doc(
        session,
        namespace="curated",
        source_doc="preferencias/mesma.md",
        document_id="doc-z",
    )

    assert found["name"] == "Documento Z"


async def test_search_entities_matches_aliases_tags_and_path_with_ranking(session):
    await age.upsert_entity(
        session,
        "Stack técnica deve ser inferida por projeto",
        "preferencia",
        "curated",
        {
            "source_doc": "preferencias/stack-tecnica-por-projeto.md",
            "repo_path": "preferencias/stack-tecnica-por-projeto.md",
            "aliases": ["stack tecnica", "stack por projeto"],
            "tags": ["arquitetura"],
        },
    )
    await age.upsert_entity(
        session,
        "Outro",
        "conceito",
        "curated",
        {
            "source_doc": "preferencias/outro-stack-tecnica.md",
            "repo_path": "preferencias/outro-stack-tecnica.md",
            "aliases": [],
            "tags": [],
        },
    )

    by_alias = await age.search_entities(session, "stack tecnica", "curated")
    assert by_alias[0]["name"] == "Stack técnica deve ser inferida por projeto"

    by_tag = await age.search_entities(session, "arquitetura", "curated")
    assert by_tag[0]["name"] == "Stack técnica deve ser inferida por projeto"

    by_path = await age.search_entities(session, "outro stack tecnica", "curated")
    assert any(entity["name"] == "Outro" for entity in by_path)


async def test_search_entities_limit_applies_after_ranking(session):
    await age.upsert_entity(
        session,
        "Alvo Exato",
        "conceito",
        "curated",
        {"aliases": ["termo"]},
    )
    await age.upsert_entity(
        session,
        "Termo",
        "conceito",
        "curated",
        {"aliases": []},
    )

    found = await age.search_entities(session, "termo", "curated", limit=1)

    assert found == [{"name": "Termo", "type": "conceito", "namespace": "curated"}]


async def test_search_entities_name_match_not_excluded_by_path_candidate_cap(session):
    for idx in range(101):
        await age.upsert_entity(
            session,
            f"A caminho {idx:03d}",
            "conceito",
            "curated",
            {
                "source_doc": f"preferencias/termo-baixa-relevancia-{idx:03d}.md",
                "repo_path": f"preferencias/termo-baixa-relevancia-{idx:03d}.md",
            },
            commit=False,
        )
    await age.upsert_entity(session, "Termo", "conceito", "curated", commit=False)
    await session.commit()

    found = await age.search_entities(session, "termo", "curated", limit=1)

    assert found == [{"name": "Termo", "type": "conceito", "namespace": "curated"}]


async def test_search_entities_exact_name_not_excluded_by_name_contains_cap(session):
    for idx in range(101):
        await age.upsert_entity(
            session,
            f"A termo {idx:03d}",
            "conceito",
            "curated",
            commit=False,
        )
    await age.upsert_entity(session, "Termo", "conceito", "curated", commit=False)
    await session.commit()

    found = await age.search_entities(session, "termo", "curated", limit=1)

    assert found == [{"name": "Termo", "type": "conceito", "namespace": "curated"}]


async def test_search_entities_exact_alias_not_excluded_by_alias_contains_cap(session):
    for idx in range(101):
        await age.upsert_entity(
            session,
            f"A alias parcial {idx:03d}",
            "conceito",
            "curated",
            {"aliases": [f"term {idx:03d}"]},
            commit=False,
        )
    await age.upsert_entity(
        session,
        "Zulu Alias Exato",
        "conceito",
        "curated",
        {"aliases": ["term"]},
        commit=False,
    )
    await session.commit()

    found = await age.search_entities(session, "term", "curated", limit=1)

    assert found == [{"name": "Zulu Alias Exato", "type": "conceito", "namespace": "curated"}]


async def test_search_entities_exact_tag_not_excluded_by_tag_contains_cap(session):
    for idx in range(101):
        await age.upsert_entity(
            session,
            f"A tag parcial {idx:03d}",
            "conceito",
            "curated",
            {"tags": [f"term {idx:03d}"]},
            commit=False,
        )
    await age.upsert_entity(
        session,
        "Zulu Tag Exata",
        "conceito",
        "curated",
        {"tags": ["term"]},
        commit=False,
    )
    await session.commit()

    found = await age.search_entities(session, "term", "curated", limit=1)

    assert found == [{"name": "Zulu Tag Exata", "type": "conceito", "namespace": "curated"}]


async def test_upsert_and_update_entity_store_normalized_search_text(session):
    await age.upsert_entity(
        session,
        "Goiânia Stack",
        "conceito",
        "curated",
        {
            "source_doc": "preferencias/goiania-stack.md",
            "repo_path": "preferencias/goiania-stack.md",
            "aliases": ["Stack Técnica"],
            "tags": ["Arquitetura"],
        },
    )

    got = await age.get_entity(session, "Goiânia Stack", "curated")
    search_text = got["props"]["search_text_normalized"]
    assert "goiania stack" in search_text
    assert "stack tecnica" in search_text
    assert "arquitetura" in search_text
    assert "preferencias goiania stack.md" in search_text
    assert got["props"]["aliases_exact_normalized"] == "|stack tecnica|"
    assert got["props"]["tags_exact_normalized"] == "|arquitetura|"

    await age.update_entity(
        session,
        "Goiânia Stack",
        "curated",
        {
            "source_doc": "preferencias/goiania-stack.md",
            "aliases": ["Nome Atualizado"],
            "tags": ["Decisão"],
        },
    )

    updated = await age.get_entity(session, "Goiânia Stack", "curated")
    updated_search_text = updated["props"]["search_text_normalized"]
    assert "goiania stack" in updated_search_text
    assert "nome atualizado" in updated_search_text
    assert "decisao" in updated_search_text


async def test_search_entities_uses_bounded_candidate_query(monkeypatch):
    class EmptyRows:
        def all(self):
            return []

    class FakeSession:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(str(statement))
            return EmptyRows()

    async def noop_prepare(session):
        return None

    monkeypatch.setattr(age, "_prepare", noop_prepare)
    fake_session = FakeSession()

    await age.search_entities(fake_session, "stack tecnica", "curated", limit=2)

    candidate_queries = fake_session.statements
    assert len(candidate_queries) >= 5
    assert all("WHERE" in query for query in candidate_queries)
    assert all("LIMIT 100" in query for query in candidate_queries)
    assert any("n.name_normalized =" in query for query in candidate_queries)
    assert any("STARTS WITH" in query for query in candidate_queries)
    assert any("n.name" in query for query in candidate_queries)
    assert any("aliases_exact_normalized" in query for query in candidate_queries)
    assert any("aliases_search_text_normalized" in query for query in candidate_queries)
    assert any("tags_exact_normalized" in query for query in candidate_queries)
    assert any("n.props.search_text_normalized" in query for query in candidate_queries)
    assert any("n.source_doc" in query for query in candidate_queries)
