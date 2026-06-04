_SYSTEM = (
    "Você extrai entidades e relações de um texto para um grafo de conhecimento. "
    "Retorne JSON no formato "
    '{"entities": [{"name": "<nome>", "type": "<pessoa|projeto|conceito|lugar|org>"}], '
    '"relations": [{"source": "<nome>", "target": "<nome>", "type": "<verbo_curto>"}]}. '
    "Use nomes canônicos e consistentes. Relações devem referenciar entidades listadas."
)


async def extract_entities(llm, text: str) -> dict:  # noqa: ANN001
    data = await llm.complete_json(_SYSTEM, text)
    entities = [
        {"name": e["name"].strip(), "type": (e.get("type") or "conceito").strip()}
        for e in data.get("entities", [])
        if (e.get("name") or "").strip()
    ]
    relations = [
        {
            "source": r["source"].strip(),
            "target": r["target"].strip(),
            "type": (r.get("type") or "related_to").strip(),
        }
        for r in data.get("relations", [])
        if (r.get("source") or "").strip() and (r.get("target") or "").strip()
    ]
    return {"entities": entities, "relations": relations}
