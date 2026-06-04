import json

from openai import AsyncOpenAI

from brain.config import Settings


class LLMClient:
    def __init__(self, client, model: str) -> None:  # noqa: ANN001
        self._client = client
        self._model = model

    @classmethod
    def from_settings(cls, settings: Settings) -> "LLMClient":
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        return cls(client, settings.extraction_model)

    async def complete_json(self, system: str, user: str) -> dict:
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
