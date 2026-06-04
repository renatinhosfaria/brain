from openai import AsyncOpenAI

from brain.config import Settings


class Embedder:
    def __init__(self, client, model: str, dim: int) -> None:  # noqa: ANN001
        self._client = client
        self._model = model
        self._dim = dim

    @classmethod
    def from_settings(cls, settings: Settings) -> "Embedder":
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        return cls(client, settings.embedding_model, settings.embedding_dim)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._client.embeddings.create(
            model=self._model, input=texts, dimensions=self._dim
        )
        return [item.embedding for item in resp.data]
