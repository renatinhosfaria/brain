import json

from brain.extraction.llm import LLMClient


class _FakeChat:
    def __init__(self, payload):
        self._payload = payload

    async def create(self, *, model, messages, response_format):
        class _Msg:
            content = json.dumps(self._payload)

        class _Choice:
            message = _Msg()

        class _R:
            choices = [_Choice()]

        return _R()


class _FakeCompletions:
    def __init__(self, payload):
        self.completions = _FakeChat(payload)


class _FakeClient:
    def __init__(self, payload):
        self.chat = _FakeCompletions(payload)


async def test_complete_json_parseia_resposta():
    client = _FakeClient({"facts": [{"content": "x"}]})
    llm = LLMClient(client=client, model="gpt-4o-mini")
    out = await llm.complete_json("sys", "user")
    assert out == {"facts": [{"content": "x"}]}
