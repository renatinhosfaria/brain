import json

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from brain.config import Settings

_RETRYABLE_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError, TimeoutError)


class LLMClient:
    def __init__(self, client, model: str, retry_attempts: int = 3) -> None:  # noqa: ANN001
        self._client = client
        self._model = model
        self._retry_attempts = retry_attempts

    @classmethod
    def from_settings(cls, settings: Settings) -> "LLMClient":
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        return cls(client, settings.extraction_model)

    async def complete_json(self, system: str, user: str) -> dict:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_RETRYABLE_ERRORS),
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(multiplier=0.1, min=0, max=2),
            reraise=True,
        ):
            with attempt:
                resp = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format={"type": "json_object"},
                )
                break
        return json.loads(resp.choices[0].message.content)
