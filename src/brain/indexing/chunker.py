import re

import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")
_HEADING = re.compile(r"^#{1,6} ", re.MULTILINE)


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def _split_by_headings(text: str) -> list[str]:
    positions = [m.start() for m in _HEADING.finditer(text)]
    if not positions:
        return [text.strip()] if text.strip() else []
    if positions[0] != 0:
        positions = [0, *positions]
    sections = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        section = text[start:end].strip()
        if section:
            sections.append(section)
    return sections


def _split_by_tokens(text: str, max_tokens: int, overlap: int) -> list[tuple[str, int]]:
    # Retorna (texto_do_pedaço, n_tokens_da_janela). Usamos o tamanho da janela
    # fatiada como token_count (sempre <= max_tokens). Re-encodar o texto
    # decodificado não serve: o BPE pode somar 1-2 tokens nas bordas e estourar
    # o limite.
    tokens = _enc.encode(text)
    if len(tokens) <= max_tokens:
        return [(text, len(tokens))]
    pieces = []
    step = max(1, max_tokens - overlap)
    for start in range(0, len(tokens), step):
        window = tokens[start : start + max_tokens]
        pieces.append((_enc.decode(window).strip(), len(window)))
        if start + max_tokens >= len(tokens):
            break
    return pieces


def chunk_markdown(text: str, max_tokens: int = 512, overlap: int = 64) -> list[dict]:
    chunks: list[dict] = []
    ordinal = 0
    for section in _split_by_headings(text):
        for piece, token_count in _split_by_tokens(section, max_tokens, overlap):
            chunks.append({"ordinal": ordinal, "text": piece, "token_count": token_count})
            ordinal += 1
    return chunks
