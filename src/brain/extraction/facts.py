_SYSTEM = (
    "Você extrai fatos memoráveis e duráveis sobre o usuário e seu contexto a partir "
    "de uma conversa. Retorne JSON no formato "
    '{"facts": [{"content": "<fato conciso na 3a pessoa>", "confidence": <0..1>}]}. '
    "Inclua apenas fatos estáveis e úteis no futuro (preferências, decisões, identidade, "
    "relações). Ignore conversa trivial. Se não houver nada memorável, retorne lista vazia."
)


def _render(messages: list[dict]) -> str:
    return "\n".join(f"{m['role']}: {m['content']}" for m in messages)


async def extract_facts(llm, messages: list[dict]) -> list[dict]:  # noqa: ANN001
    data = await llm.complete_json(_SYSTEM, _render(messages))
    result = []
    for f in data.get("facts", []):
        content = (f.get("content") or "").strip()
        if not content:
            continue
        result.append({"content": content, "confidence": float(f.get("confidence", 1.0))})
    return result
