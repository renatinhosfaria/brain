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


class _FlakyChat:
    def __init__(self):
        self.calls = 0

    async def create(self, *, model, messages, response_format):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("temporario")

        class _Msg:
            content = json.dumps({"ok": True})

        class _Choice:
            message = _Msg()

        class _R:
            choices = [_Choice()]

        return _R()


class _FlakyCompletions:
    def __init__(self):
        self.completions = _FlakyChat()


class _FlakyClient:
    def __init__(self):
        self.chat = _FlakyCompletions()


async def test_complete_json_parseia_resposta():
    client = _FakeClient({"facts": [{"content": "x"}]})
    llm = LLMClient(client=client, model="gpt-4o-mini")
    out = await llm.complete_json("sys", "user")
    assert out == {"facts": [{"content": "x"}]}


async def test_complete_json_retenta_falha_transitoria():
    client = _FlakyClient()
    llm = LLMClient(client=client, model="gpt-4o-mini")
    out = await llm.complete_json("sys", "user")
    assert out == {"ok": True}
    assert client.chat.completions.calls == 2
