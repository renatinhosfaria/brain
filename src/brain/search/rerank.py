"""Reranking opcional do top-k vetorial usando o LLM já configurado.

A busca vetorial recupera um conjunto amplo de candidatos; o reranker reordena
esse conjunto por relevância semântica à consulta e devolve os `top_n` melhores.
É opt-in (`RERANK_ENABLED`) e degrada com segurança: qualquer falha do LLM ou
resposta malformada faz cair de volta na ordem vetorial original.
"""

_RERANK_SYSTEM = (
    "Você reordena trechos por relevância para a consulta do usuário. "
    "Para cada trecho, atribua um score de relevância entre 0 e 1 "
    "(1 = altamente relevante; 0 = irrelevante). "
    'Responda APENAS JSON no formato {"scores": [{"index": <int>, "score": <float>}]}, '
    "usando exatamente o índice indicado entre colchetes em cada trecho."
)

_MAX_SNIPPET_CHARS = 1000


def _parse_scores(data: dict, n: int) -> dict[int, float]:
    scores: dict[int, float] = {}
    if not isinstance(data, dict):
        return scores
    raw = data.get("scores")
    if not isinstance(raw, list):
        return scores
    for item in raw:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        score = item.get("score")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            continue
        if 0 <= index < n:
            scores[index] = float(score)
    return scores


def _build_user_prompt(query: str, results: list[dict]) -> str:
    blocks = []
    for i, r in enumerate(results):
        snippet = str(r.get("text") or "")[:_MAX_SNIPPET_CHARS]
        blocks.append(f"[{i}] {snippet}")
    return f"Consulta: {query}\n\nTrechos:\n" + "\n\n".join(blocks)


async def rerank(llm, query: str, results: list[dict], *, top_n: int) -> list[dict]:  # noqa: ANN001
    """Reordena `results` por relevância via LLM e retorna os `top_n` melhores.

    Mantém a ordem vetorial original (cortada em `top_n`) se o LLM estiver ausente,
    falhar ou não devolver scores utilizáveis.
    """
    if llm is None or len(results) <= 1:
        return results[:top_n]

    try:
        data = await llm.complete_json(_RERANK_SYSTEM, _build_user_prompt(query, results))
    except Exception:  # noqa: BLE001 — reranking é best-effort; cai no ranqueamento vetorial
        return results[:top_n]

    scores = _parse_scores(data, len(results))
    if not scores:
        return results[:top_n]

    for i, r in enumerate(results):
        if i in scores:
            r["rerank_score"] = scores[i]

    # Estável: empates e itens sem score do LLM preservam a ordem vetorial.
    order = sorted(range(len(results)), key=lambda i: scores.get(i, -1.0), reverse=True)
    return [results[i] for i in order][:top_n]
