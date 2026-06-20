from brain.ingestion.semantic_entities import (
    build_curated_entity_payload,
    normalize_entity_text,
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
    assert "regras-env-e-migrations-por-projeto" in _aliases(payload)
    assert "regras env e migrations por projeto" in _aliases(payload)
    assert "env migrations" in _aliases(payload)
    assert ".env" in _aliases(payload)


def test_build_payload_uses_h1_before_humanized_path():
    payload = build_curated_entity_payload(
        namespace="curated",
        repo_path="preferencias/stack-tecnica-por-projeto.md",
        title="Stack técnica deve ser inferida por projeto",
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
