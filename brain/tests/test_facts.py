from brain.extraction.facts import extract_facts


class _FakeLLM:
    def __init__(self, payload):
        self._payload = payload
        self.last_user = None

    async def complete_json(self, system, user):
        self.last_user = user
        return self._payload


async def test_extrai_fatos_normalizados():
    llm = _FakeLLM({"facts": [
        {"content": "usuário prefere TypeScript", "confidence": 0.9},
        {"content": "", "confidence": 0.5},  # descartado: vazio
        {"content": "mora em Goiânia"},        # confidence default 1.0
    ]})
    msgs = [{"role": "user", "content": "eu prefiro typescript e moro em goiânia"}]
    facts = await extract_facts(llm, msgs)
    assert {"content": "usuário prefere TypeScript", "confidence": 0.9} in facts
    assert {"content": "mora em Goiânia", "confidence": 1.0} in facts
    assert len(facts) == 2  # vazio foi removido
    assert "user:" in llm.last_user  # a conversa foi renderizada no prompt


async def test_sem_fatos_retorna_vazio():
    llm = _FakeLLM({"facts": []})
    assert await extract_facts(llm, [{"role": "user", "content": "oi"}]) == []
