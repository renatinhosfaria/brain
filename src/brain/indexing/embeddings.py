from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from brain.config import Settings

_RETRYABLE_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError, TimeoutError)


class Embedder:
    def __init__(
        self,
        client,
        model: str,
        dim: int,
        retry_attempts: int = 3,  # noqa: ANN001
    ) -> None:
        self._client = client
        self._model = model
        self._dim = dim
        self._retry_attempts = retry_attempts

    @classmethod
    def from_settings(cls, settings: Settings) -> "Embedder":
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        return cls(client, settings.embedding_model, settings.embedding_dim)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_RETRYABLE_ERRORS),
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(multiplier=0.1, min=0, max=2),
            reraise=True,
        ):
            with attempt:
                resp = await self._client.embeddings.create(
                    model=self._model, input=texts, dimensions=self._dim
                )
                break
        return [item.embedding for item in resp.data]
