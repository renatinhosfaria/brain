from brain.search.rerank import rerank


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def complete_json(self, system, user):
        self.calls = self.calls + 1
        return self.payload


class BoomLLM:
    async def complete_json(self, system, user):
        raise RuntimeError("falha do provedor")


def _results():
    return [
        {"id": "a", "text": "primeiro", "score": 0.9},
        {"id": "b", "text": "segundo", "score": 0.8},
        {"id": "c", "text": "terceiro", "score": 0.7},
    ]


async def test_reordena_por_score_do_llm():
    llm = FakeLLM(
        {
            "scores": [
                {"index": 0, "score": 0.1},
                {"index": 1, "score": 0.5},
                {"index": 2, "score": 0.99},
            ]
        }
    )
    out = await rerank(llm, "q", _results(), top_n=3)
    assert [r["id"] for r in out] == ["c", "b", "a"]
    assert out[0]["rerank_score"] == 0.99


async def test_itens_sem_score_do_llm_vao_para_o_fim_preservando_ordem():
    llm = FakeLLM({"scores": [{"index": 2, "score": 0.99}]})
    out = await rerank(llm, "q", _results(), top_n=3)
    assert [r["id"] for r in out] == ["c", "a", "b"]


async def test_corta_em_top_n():
    llm = FakeLLM({"scores": [{"index": 0, "score": 0.2}, {"index": 1, "score": 0.9}]})
    out = await rerank(llm, "q", _results(), top_n=1)
    assert [r["id"] for r in out] == ["b"]


async def test_fallback_quando_llm_falha():
    out = await rerank(BoomLLM(), "q", _results(), top_n=2)
    assert [r["id"] for r in out] == ["a", "b"]


async def test_fallback_quando_resposta_invalida():
    out = await rerank(FakeLLM({"lixo": 1}), "q", _results(), top_n=3)
    assert [r["id"] for r in out] == ["a", "b", "c"]


async def test_sem_llm_retorna_ordem_original():
    out = await rerank(None, "q", _results(), top_n=2)
    assert [r["id"] for r in out] == ["a", "b"]


async def test_lista_unitaria_nao_chama_llm():
    llm = FakeLLM({"scores": [{"index": 0, "score": 0.5}]})
    out = await rerank(llm, "q", [{"id": "a", "text": "x", "score": 0.5}], top_n=5)
    assert [r["id"] for r in out] == ["a"]
    assert llm.calls == 0
