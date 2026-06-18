import pytest

from brain.extraction.query_entities import extract_query_entities


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def complete_json(self, system, user):
        self.calls.append({"system": system, "user": user})
        return self.payload


@pytest.mark.asyncio
async def test_extract_query_entities_limita_deduplica_e_remove_vazios():
    llm = FakeLLM(
        {
            "entities": [
                "brain",
                {"name": "Hermes"},
                {"name": "brain"},
                {"name": "  "},
                {"name": "Vault"},
            ]
        }
    )

    out = await extract_query_entities(llm, "Como Hermes se relaciona com brain?", 2)

    assert out == ["brain", "Hermes"]
    assert "no maximo 2" in llm.calls[0]["system"]


@pytest.mark.asyncio
async def test_extract_query_entities_sem_llm_retorna_lista_vazia():
    assert await extract_query_entities(None, "brain", 3) == []
