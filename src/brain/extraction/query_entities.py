def _system_prompt(max_entities: int) -> str:
    return (
        "Extraia no maximo "
        f"{max_entities} entidades-chave da pergunta do usuario para buscar em um grafo "
        "de conhecimento. Retorne JSON no formato "
        '{"entities": [{"name": "nome canonico"}]}. '
        "Inclua apenas nomes de pessoas, projetos, organizacoes, lugares ou conceitos "
        "centrais. Nao explique a resposta."
    )


def _entity_name(item) -> str | None:  # noqa: ANN001
    if isinstance(item, str):
        value = item
    elif isinstance(item, dict):
        value = item.get("name")
        if value is None:
            value = item.get("entity")
    else:
        return None
    if value is None:
        return None
    value = str(value).strip()
    return value or None


async def extract_query_entities(llm, query: str, max_entities: int) -> list[str]:  # noqa: ANN001
    if llm is None or max_entities <= 0:
        return []

    data = await llm.complete_json(_system_prompt(max_entities), query)
    items = data.get("entities") or []
    if not isinstance(items, list):
        items = []
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        name = _entity_name(item)
        if name is None:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
        if len(result) >= max_entities:
            break
    return result
