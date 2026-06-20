import pytest

import brain.ingestion.semantic_entities as semantic_entities
from brain.ingestion.semantic_entities import (
    build_curated_entity_payload,
    normalize_entity_text,
    upsert_entity_from_curated_document,
)


def _aliases(payload: dict) -> set[str]:
    return set(payload["props"]["aliases"])


def test_build_payload_prefers_metadata_title_over_h1_and_keeps_path_alias():
    payload = build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/regras-env-e-migrations-por-projeto.md",
        title="H1 ignorado",
        content="# H1 ignorado\n\nCorpo.",
        metadata={
            "title": "Regras de .env e migrations dependem do projeto",
            "type": "preference",
            "tags": ["env", "migrations"],
        },
        document_id="doc-1",
    )

    assert payload["status"] == "ready"
    assert payload["name"] == "Regras de .env e migrations dependem do projeto"
    assert payload["type"] == "preferencia"
    assert payload["props"]["source_doc"] == "preferencias/regras-env-e-migrations-por-projeto.md"
    assert payload["props"]["repo_path"] == "preferencias/regras-env-e-migrations-por-projeto.md"
    assert payload["props"]["document_id"] == "doc-1"
    assert payload["props"]["tags"] == ["env", "migrations"]
    assert {
        ".env",
        "env",
        "migrations",
        "env migrations",
        "regras env",
        "migrations por projeto",
        "regras de env e migrations",
        "regras-env-e-migrations-por-projeto",
        "regras env e migrations por projeto",
    }.issubset(_aliases(payload))


def test_build_payload_uses_markdown_h1_before_humanized_path_when_title_is_none():
    payload = build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/stack-tecnica-por-projeto.md",
        title=None,
        content="# Stack técnica deve ser inferida por projeto\n\nCorpo.",
        metadata={},
    )

    assert payload["status"] == "ready"
    assert payload["name"] == "Stack técnica deve ser inferida por projeto"
    assert "stack técnica" in _aliases(payload)
    assert "stack tecnica" in _aliases(payload)
    assert "stack por projeto" in _aliases(payload)
    assert "stack técnica por projeto" in _aliases(payload)
    assert "stack tecnica por projeto" in _aliases(payload)


def test_build_payload_uses_humanized_path_without_title_or_h1():
    payload = build_curated_entity_payload(
        namespace="curated",
        repo_path="conceitos/minio.md",
        title=None,
        content="Sem heading.",
        metadata={},
    )

    assert payload["status"] == "ready"
    assert payload["name"] == "Minio"
    assert "minio" in _aliases(payload)


def test_build_payload_skips_when_no_canonical_name_can_be_derived():
    assert build_curated_entity_payload(
        namespace="curated",
        repo_path=".md",
        title=None,
        content="Sem heading.",
        metadata={},
    ) == {"status": "skipped", "reason": "missing_name"}


def test_build_payload_skips_non_curated_agents_and_non_markdown():
    assert build_curated_entity_payload(
        namespace="tenant",
        repo_path="preferencias/privacidade.md",
        title="Privacidade",
        content="# Privacidade",
        metadata={},
    ) == {"status": "skipped", "reason": "namespace_not_curated"}

    assert build_curated_entity_payload(
        namespace="curated",
        repo_path="_agents/chatgpt/raw.md",
        title="Raw",
        content="# Raw",
        metadata={},
    ) == {"status": "skipped", "reason": "agent_inbox_path"}

    assert build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/raw.txt",
        title="Raw",
        content="# Raw",
        metadata={},
    ) == {"status": "skipped", "reason": "not_markdown"}


def test_alias_examples_are_conservative_and_domain_aliases_are_explicit():
    privacy = build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/privacidade-credenciais-e-acoes-externas.md",
        title="Privacidade, credenciais e ações externas",
        content="# Privacidade, credenciais e ações externas\n\nCorpo.",
        metadata={"title": "Privacidade, credenciais e ações externas"},
    )
    assert {
        "privacidade",
        "credenciais",
        "ações externas",
        "acoes externas",
        "privacidade credenciais",
        "privacidade credenciais acoes externas",
    }.issubset(_aliases(privacy))
    assert "regras" not in _aliases(privacy)

    ceo = build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/perfil-ceo.md",
        title="Perfil CEO",
        content="# Perfil CEO\n\nCorpo.",
        metadata={"aliases": ["Hermes CEO", "ceo hermes"]},
    )
    assert {"CEO", "perfil ceo", "Hermes CEO", "ceo hermes"}.issubset(_aliases(ceo))


def test_generic_metadata_tags_and_aliases_do_not_emit_singleton_aliases():
    payload = build_curated_entity_payload(
        namespace="curated",
        repo_path="conceitos/atalhos.md",
        title="Atalhos úteis",
        content="# Atalhos úteis\n\nCorpo.",
        metadata={
            "tags": ["regras", "perfil", "tecnica", "deve", ".env", "env", "migrations"],
            "aliases": ["regras", "perfil", "tecnica", "deve", "CEO"],
        },
    )

    aliases = _aliases(payload)
    assert {"regras", "perfil", "tecnica", "deve"}.isdisjoint(aliases)
    assert {".env", "env", "CEO", "migrations"}.issubset(aliases)


def test_type_mapping_and_raw_type_preservation():
    mapped = build_curated_entity_payload(
        namespace="curated",
        repo_path="decisoes/x.md",
        title="Decisão X",
        content="# Decisão X",
        metadata={"type": "decision"},
    )
    assert mapped["type"] == "decisao"
    assert "raw_type" not in mapped["props"]

    unknown = build_curated_entity_payload(
        namespace="curated",
        repo_path="notas/x.md",
        title="Nota X",
        content="# Nota X",
        metadata={"type": "playbook"},
    )
    assert unknown["type"] == "conceito"
    assert unknown["props"]["raw_type"] == "playbook"


def test_normalize_entity_text_casefolds_and_removes_accents():
    assert normalize_entity_text("Stack Técnica") == "stack tecnica"
    assert normalize_entity_text("ações externas") == "acoes externas"
    assert normalize_entity_text("regras-env") == "regras env"
    assert normalize_entity_text(".env") == ".env"
    assert normalize_entity_text("  Stack   Técnica\npor\tprojeto  ") == "stack tecnica por projeto"


@pytest.mark.asyncio
async def test_upsert_returns_skipped_payload_without_calling_age(monkeypatch):
    calls = []

    async def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("AGE helper should not be called for skipped payload")

    monkeypatch.setattr(
        semantic_entities.age,
        "find_entity_by_source_doc",
        fail_if_called,
        raising=False,
    )
    monkeypatch.setattr(
        semantic_entities.age,
        "update_entity_identity",
        fail_if_called,
        raising=False,
    )
    monkeypatch.setattr(
        semantic_entities.age,
        "upsert_entity",
        fail_if_called,
        raising=False,
    )

    result = await upsert_entity_from_curated_document(
        object(),
        namespace="tenant",
        repo_path="preferencias/privacidade.md",
        title="Privacidade",
        content="# Privacidade",
        metadata={},
    )

    assert result == {"status": "skipped", "reason": "namespace_not_curated"}
    assert calls == []


@pytest.mark.asyncio
async def test_upsert_updates_existing_entity_with_commit_false(monkeypatch):
    session = object()
    existing = {"id": "entity-1"}
    calls = []

    async def find_entity_by_source_doc(session_arg, **kwargs):
        calls.append(("find", session_arg, kwargs))
        return existing

    async def update_entity_identity(session_arg, **kwargs):
        calls.append(("update", session_arg, kwargs))

    async def fail_upsert(*args, **kwargs):
        calls.append(("upsert", args, kwargs))
        raise AssertionError("upsert_entity should not be called for existing entity")

    monkeypatch.setattr(
        semantic_entities.age,
        "find_entity_by_source_doc",
        find_entity_by_source_doc,
        raising=False,
    )
    monkeypatch.setattr(
        semantic_entities.age,
        "update_entity_identity",
        update_entity_identity,
        raising=False,
    )
    monkeypatch.setattr(
        semantic_entities.age,
        "upsert_entity",
        fail_upsert,
        raising=False,
    )

    result = await upsert_entity_from_curated_document(
        session,
        namespace="curated",
        repo_path="preferencias/perfil-ceo.md",
        title="Perfil CEO",
        content="# Perfil CEO",
        metadata={},
    )

    assert result["status"] == "updated"
    assert calls == [
        (
            "find",
            session,
            {
                "namespace": "curated",
                "source_doc": "preferencias/perfil-ceo.md",
            },
        ),
        (
            "update",
            session,
            {
                "entity": existing,
                "namespace": "curated",
                "name": "Perfil CEO",
                "type": "conceito",
                "props": result["props"],
                "commit": False,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_upsert_creates_entity_with_commit_false_when_not_found(monkeypatch):
    session = object()
    calls = []

    async def find_entity_by_source_doc(session_arg, **kwargs):
        calls.append(("find", session_arg, kwargs))
        return None

    async def fail_update(*args, **kwargs):
        calls.append(("update", args, kwargs))
        raise AssertionError("update_entity_identity should not be called for new entity")

    async def upsert_entity(*args, **kwargs):
        calls.append(("upsert", args, kwargs))

    monkeypatch.setattr(
        semantic_entities.age,
        "find_entity_by_source_doc",
        find_entity_by_source_doc,
        raising=False,
    )
    monkeypatch.setattr(
        semantic_entities.age,
        "update_entity_identity",
        fail_update,
        raising=False,
    )
    monkeypatch.setattr(
        semantic_entities.age,
        "upsert_entity",
        upsert_entity,
        raising=False,
    )

    result = await upsert_entity_from_curated_document(
        session,
        namespace="curated",
        repo_path="preferencias/perfil-ceo.md",
        title="Perfil CEO",
        content="# Perfil CEO",
        metadata={},
    )

    assert result["status"] == "created"
    assert calls == [
        (
            "find",
            session,
            {
                "namespace": "curated",
                "source_doc": "preferencias/perfil-ceo.md",
            },
        ),
        (
            "upsert",
            (
                session,
                "Perfil CEO",
                "conceito",
                "curated",
                result["props"],
            ),
            {"commit": False},
        ),
    ]
