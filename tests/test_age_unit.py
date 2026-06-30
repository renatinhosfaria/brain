"""Testes unitários para brain.graph.age — funções puras, sem banco."""

import re

from brain.graph import age


def test_lit_preserva_dollar_quote():
    """_lit() não escapa $cy$ — root cause da injeção via dollar-quote do PostgreSQL.

    json.dumps escapa aspas e barras para Cypher, mas não toca a sequência $cy$
    usada como tag de dollar-quote no SQL gerado. _safe_cypher() é a defesa real.
    """
    result = age._lit("injecao $cy$) AS (v agtype); DROP TABLE documents; --")
    assert "$cy$" in result


class TestSafeCypher:
    def test_gera_tag_aleatoria(self):
        q = age._safe_cypher("MATCH (n) RETURN n", "AS (n agtype)")
        assert "$cy$" not in q
        assert re.search(r"\$cy_[0-9a-f]{16}\$", q)

    def test_formato_correto(self):
        body = "MATCH (n:Entity) RETURN n.name"
        q = age._safe_cypher(body, "AS (name agtype)")
        m = re.search(r"\$cy_[0-9a-f]{16}\$", q)
        assert m
        tag = m.group()
        assert q == f"SELECT * FROM cypher('brain', {tag} {body} {tag}) AS (name agtype)"

    def test_tag_ausente_no_body(self):
        """A tag gerada não deve aparecer dentro do corpo — propriedade anti-injeção."""
        body = "MATCH (n:Entity) RETURN n"
        q = age._safe_cypher(body, "AS (n agtype)")
        m = re.search(r"\$cy_[0-9a-f]{16}\$", q)
        assert m
        tag = m.group()
        first_idx = q.index(tag)
        last_idx = q.rindex(tag)
        inner = q[first_idx + len(tag) : last_idx]
        assert tag not in inner

    def test_body_com_dollar_quote_fixo_continua_seguro(self):
        """Body contendo '$cy$' ainda produz query segura — vetor bloqueado."""
        malicious_val = age._lit("x $cy$) AS (v agtype); DROP TABLE documents; --")
        body = f"MATCH (n:Entity {{name: {malicious_val}}}) RETURN n"
        assert "$cy$" in body  # confirma que o vetor de injeção está no body
        q = age._safe_cypher(body, "AS (n agtype)")
        m = re.search(r"\$cy_[0-9a-f]{16}\$", q)
        assert m
        tag = m.group()
        first_idx = q.index(tag)
        last_idx = q.rindex(tag)
        inner = q[first_idx + len(tag) : last_idx]
        assert tag not in inner

    def test_tags_distintas_entre_chamadas(self):
        """Duas chamadas consecutivas devem (quase sempre) gerar tags diferentes."""
        body = "MATCH (n) RETURN n"
        tags = set()
        for _ in range(20):
            q = age._safe_cypher(body, "AS (n agtype)")
            m = re.search(r"\$cy_[0-9a-f]{16}\$", q)
            assert m
            tags.add(m.group())
        assert len(tags) > 1, "as tags deveriam variar entre chamadas"

    def test_grafo_customizado(self):
        q = age._safe_cypher("MATCH (n) RETURN n", "AS (n agtype)", graph="outro")
        assert "cypher('outro', " in q
