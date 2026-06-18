from brain.extraction.entities import extract_entities


class _FakeLLM:
    def __init__(self, payload):
        self._payload = payload

    async def complete_json(self, system, user):
        return self._payload


async def test_extrai_entidades_e_relacoes_normalizadas():
    llm = _FakeLLM({
        "entities": [
            {"name": "Renato", "type": "pessoa"},
            {"name": "brain", "type": "projeto"},
            {"name": "", "type": "x"},  # descartado
        ],
        "relations": [
            {"source": "Renato", "target": "brain", "type": "works_on"},
            {"source": "Renato", "target": "", "type": "x"},  # descartado
        ],
    })
    out = await extract_entities(llm, "Renato trabalha no brain")
    assert {"name": "Renato", "type": "pessoa"} in out["entities"]
    assert len(out["entities"]) == 2
    assert {"source": "Renato", "target": "brain", "type": "works_on"} in out["relations"]
    assert len(out["relations"]) == 1


async def test_payload_incompleto_vira_listas_vazias():
    llm = _FakeLLM({})
    out = await extract_entities(llm, "texto")
    assert out == {"entities": [], "relations": []}
