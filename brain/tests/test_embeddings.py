from brain.indexing.embeddings import Embedder


class _FakeEmbeddings:
    async def create(self, *, model, input, dimensions):  # noqa: A002
        class _D:
            def __init__(self, e):
                self.embedding = e

        class _R:
            data = [_D([0.0] * dimensions) for _ in input]

        return _R()


class _FakeClient:
    embeddings = _FakeEmbeddings()


async def test_embed_retorna_um_vetor_por_texto():
    emb = Embedder(client=_FakeClient(), model="text-embedding-3-large", dim=2000)
    vecs = await emb.embed(["a", "b", "c"])
    assert len(vecs) == 3
    assert all(len(v) == 2000 for v in vecs)


async def test_embed_lista_vazia_nao_chama_api():
    emb = Embedder(client=_FakeClient(), model="x", dim=2000)
    assert await emb.embed([]) == []
