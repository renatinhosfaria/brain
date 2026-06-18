from brain.indexing.chunker import chunk_markdown, count_tokens


def test_texto_vazio_retorna_lista_vazia():
    assert chunk_markdown("") == []


def test_texto_curto_vira_um_chunk():
    chunks = chunk_markdown("# Título\nconteúdo curto")
    assert len(chunks) == 1
    assert chunks[0]["ordinal"] == 0
    assert "conteúdo curto" in chunks[0]["text"]
    assert chunks[0]["token_count"] > 0


def test_headings_geram_secoes_separadas():
    texto = "# A\ntexto a\n\n# B\ntexto b\n\n# C\ntexto c"
    chunks = chunk_markdown(texto)
    assert len(chunks) == 3
    assert [c["ordinal"] for c in chunks] == [0, 1, 2]


def test_secao_longa_divide_com_overlap_e_ordinais_sequenciais():
    longo = "# Grande\n" + " ".join(["palavra"] * 4000)
    chunks = chunk_markdown(longo, max_tokens=200, overlap=20)
    assert len(chunks) > 1
    assert [c["ordinal"] for c in chunks] == list(range(len(chunks)))
    assert all(c["token_count"] <= 200 for c in chunks)
