from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
import re
import unicodedata

from brain.graph import age


_MARKDOWN_SUFFIXES = (".md", ".markdown")
_TYPE_MAP = {
    "project": "projeto",
    "preference": "preferencia",
    "decision": "decisao",
    "process": "processo",
    "concept": "conceito",
    "reference": "referencia",
    "map": "mapa",
}
_GENERIC_SINGLE_ALIASES = {
    "a",
    "as",
    "acao",
    "acoes",
    "ao",
    "aos",
    "da",
    "das",
    "de",
    "deve",
    "devem",
    "do",
    "dos",
    "e",
    "em",
    "inferida",
    "inferido",
    "nota",
    "notas",
    "o",
    "os",
    "perfil",
    "por",
    "projeto",
    "regra",
    "regras",
    "ser",
    "tecnica",
}
_DOMAIN_SINGLE_ALIASES = {
    ".env": ".env",
    "ceo": "CEO",
    "credenciais": "credenciais",
    "env": "env",
    "migrations": "migrations",
    "privacidade": "privacidade",
}


def normalize_entity_text(value: str) -> str:
    """Normalize entity text for matching while preserving leading-dot terms."""
    return _normalize_text(value, strip_accents=True)


def build_curated_entity_payload(
    namespace,
    repo_path,
    title,
    content,
    metadata,
    document_id=None,
) -> dict:
    metadata = metadata or {}
    path = _clean_str(repo_path)

    if namespace != "curated":
        return {"status": "skipped", "reason": "namespace_not_curated"}
    if _is_agent_path(path):
        return {"status": "skipped", "reason": "agent_inbox_path"}
    if not _is_markdown_path(path):
        return {"status": "skipped", "reason": "not_markdown"}

    metadata_title = _clean_str(metadata.get("title"))
    document_title = _clean_str(title)
    h1 = _first_h1(content)
    humanized_path = _humanized_path(path)
    name = _first_present(metadata_title, document_title, h1, humanized_path)
    if not name:
        return {"status": "skipped", "reason": "missing_name"}

    entity_type, raw_type = _entity_type(metadata.get("type"))
    tags = _as_string_list(metadata.get("tags"))
    aliases = _build_aliases(
        name=name,
        title=document_title,
        h1=h1,
        repo_path=path,
        tags=tags,
        metadata_aliases=_metadata_aliases(metadata),
    )
    props = {
        "source": "curated_note",
        "source_doc": path,
        "repo_path": path,
        "document_id": _clean_str(document_id) or None,
        "title": name,
        "tags": tags,
        "aliases": aliases,
        "name_normalized": normalize_entity_text(name),
        "aliases_normalized": _normalized_unique(aliases),
        "tags_normalized": _normalized_unique(tags),
        "repo_path_normalized": normalize_entity_text(path),
    }
    if raw_type:
        props["raw_type"] = raw_type

    return {
        "status": "ready",
        "name": name,
        "type": entity_type,
        "props": props,
    }


async def upsert_entity_from_curated_document(
    session,
    *,
    namespace,
    repo_path,
    title,
    content,
    metadata,
    document_id=None,
) -> dict:
    payload = build_curated_entity_payload(
        namespace=namespace,
        repo_path=repo_path,
        title=title,
        content=content,
        metadata=metadata,
        document_id=document_id,
    )
    if payload["status"] == "skipped":
        return payload

    existing = await age.find_entity_by_source_doc(
        session,
        namespace=namespace,
        source_doc=payload["props"]["source_doc"],
    )
    if existing:
        await age.update_entity_identity(
            session,
            entity=existing,
            namespace=namespace,
            name=payload["name"],
            type=payload["type"],
            props=payload["props"],
            commit=False,
        )
        return {**payload, "status": "updated"}

    await age.upsert_entity(
        session,
        payload["name"],
        payload["type"],
        namespace,
        payload["props"],
        commit=False,
    )
    return {**payload, "status": "created"}


def _normalize_text(value: object, *, strip_accents: bool) -> str:
    text = _clean_str(value).casefold()
    if strip_accents:
        text = "".join(
            ch
            for ch in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(ch)
        )
    text = re.sub(r"[-\u2010-\u2015]+", " ", text)

    chars: list[str] = []
    for idx, ch in enumerate(text):
        if ch == "_" or ch == "/":
            chars.append(" ")
        elif ch == "." and _keeps_dot(text, idx):
            chars.append(ch)
        elif ch.isalnum() or ch.isspace():
            chars.append(ch)
        else:
            chars.append(" ")
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def _keeps_dot(text: str, idx: int) -> bool:
    previous_is_word = idx > 0 and text[idx - 1].isalnum()
    next_is_word = idx + 1 < len(text) and text[idx + 1].isalnum()
    return next_is_word and not previous_is_word


def _clean_str(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_present(*values: str | None) -> str | None:
    for value in values:
        cleaned = _clean_str(value)
        if cleaned:
            return cleaned
    return None


def _is_markdown_path(repo_path: str) -> bool:
    return repo_path.casefold().endswith(_MARKDOWN_SUFFIXES)


def _is_agent_path(repo_path: str) -> bool:
    parts = [part for part in repo_path.replace("\\", "/").split("/") if part]
    return "_agents" in parts


def _first_h1(content: object) -> str | None:
    for line in _clean_str(content).splitlines():
        match = re.match(r"^\s*#(?!#)\s+(.+?)\s*#*\s*$", line)
        if match:
            return match.group(1).strip()
    return None


def _humanized_path(repo_path: str) -> str | None:
    filename = PurePosixPath(repo_path.replace("\\", "/")).name
    stem = _strip_markdown_suffix(filename)
    normalized = normalize_entity_text(stem)
    if not normalized:
        return None
    return " ".join(part.capitalize() for part in normalized.split())


def _strip_markdown_suffix(path: str) -> str:
    lowered = path.casefold()
    for suffix in _MARKDOWN_SUFFIXES:
        if lowered.endswith(suffix):
            return path[: -len(suffix)]
    return PurePosixPath(path).stem


def _entity_type(raw_value: object) -> tuple[str, str | None]:
    raw_type = _clean_str(raw_value)
    if not raw_type:
        return "conceito", None
    mapped = _TYPE_MAP.get(normalize_entity_text(raw_type))
    if mapped:
        return mapped, None
    return "conceito", raw_type


def _as_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[object] = [value]
    elif isinstance(value, set):
        values = sorted(value, key=str)
    elif isinstance(value, Sequence):
        values = value
    elif isinstance(value, Iterable):
        values = list(value)
    else:
        values = [value]

    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        cleaned = _clean_str(item)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _metadata_aliases(metadata: dict) -> list[str]:
    aliases: list[str] = []
    aliases.extend(_as_string_list(metadata.get("aliases")))
    aliases.extend(_as_string_list(metadata.get("alias")))
    return _unique(aliases)


def _build_aliases(
    *,
    name: str,
    title: str,
    h1: str | None,
    repo_path: str,
    tags: list[str],
    metadata_aliases: list[str],
) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()

    for phrase in _unique([name, title, h1]):
        _add_phrase_aliases(aliases, seen, phrase)
    _add_path_aliases(aliases, seen, repo_path)
    for tag in tags:
        _add_alias(aliases, seen, tag, force=True)
        normalized = normalize_entity_text(tag)
        if normalized != tag:
            _add_alias(aliases, seen, normalized, force=True)
    for alias in metadata_aliases:
        _add_alias(aliases, seen, alias, force=True)
        normalized = normalize_entity_text(alias)
        if normalized != alias:
            _add_alias(aliases, seen, normalized, force=True)

    return aliases


def _add_phrase_aliases(aliases: list[str], seen: set[str], value: object) -> None:
    raw = _clean_str(value)
    if not raw:
        return

    display = _normalize_text(raw, strip_accents=False)
    normalized = normalize_entity_text(raw)
    _add_alias(aliases, seen, raw)
    _add_alias(aliases, seen, display)
    _add_alias(aliases, seen, normalized)

    tokens = _tokens(raw)
    _add_domain_token_aliases(aliases, seen, tokens)
    _add_keyphrase_aliases(aliases, seen, tokens)


def _add_path_aliases(aliases: list[str], seen: set[str], repo_path: str) -> None:
    filename = PurePosixPath(repo_path.replace("\\", "/")).name
    stem = _strip_markdown_suffix(filename)
    if not stem:
        return
    _add_alias(aliases, seen, stem)
    normalized = normalize_entity_text(stem)
    _add_alias(aliases, seen, normalized)
    tokens = _tokens(stem)
    _add_domain_token_aliases(aliases, seen, tokens)
    _add_keyphrase_aliases(aliases, seen, tokens)


def _tokens(value: str) -> list[tuple[str, str]]:
    display = _normalize_text(value, strip_accents=False)
    tokens: list[tuple[str, str]] = []
    for token in display.split():
        normalized = normalize_entity_text(token)
        if normalized:
            tokens.append((token, normalized))
    return tokens


def _add_domain_token_aliases(
    aliases: list[str],
    seen: set[str],
    tokens: list[tuple[str, str]],
) -> None:
    for _display, normalized in tokens:
        alias = _DOMAIN_SINGLE_ALIASES.get(normalized)
        if alias:
            _add_alias(aliases, seen, alias, force=True)
        if normalized.startswith("."):
            bare = normalized.removeprefix(".")
            alias = _DOMAIN_SINGLE_ALIASES.get(bare)
            if alias:
                _add_alias(aliases, seen, alias, force=True)


def _add_keyphrase_aliases(
    aliases: list[str],
    seen: set[str],
    tokens: list[tuple[str, str]],
) -> None:
    norms = [normalized for _display, normalized in tokens]

    for sequence in (
        ("acoes", "externas"),
        ("privacidade", "credenciais"),
        ("stack", "tecnica"),
    ):
        _add_adjacent_sequence(aliases, seen, tokens, sequence)

    if _contains_in_order(norms, ("privacidade", "credenciais", "acoes", "externas")):
        _add_alias(aliases, seen, "privacidade credenciais acoes externas")

    has_env = ".env" in norms or "env" in norms
    if has_env and "migrations" in norms:
        _add_alias(aliases, seen, "env migrations")
        _add_env_migration_aliases(aliases, seen, norms)

    if "stack" in norms and "projeto" in norms:
        tecnica_display = _display_for(tokens, "tecnica")
        if tecnica_display:
            _add_alias(aliases, seen, f"stack {tecnica_display}")
            _add_alias(aliases, seen, "stack tecnica")
            _add_alias(aliases, seen, f"stack {tecnica_display} por projeto")
            _add_alias(aliases, seen, "stack tecnica por projeto")
        _add_alias(aliases, seen, "stack por projeto")


def _add_env_migration_aliases(
    aliases: list[str],
    seen: set[str],
    norms: list[str],
) -> None:
    phrase_tokens = ["env" if token == ".env" else token for token in norms]
    env_idx = _first_index(norms, {".env", "env"})
    migrations_idx = _first_index(norms, {"migrations"})
    if env_idx is None or migrations_idx is None or migrations_idx < env_idx:
        return

    regras_idx = _first_index(norms, {"regras"})
    if regras_idx is not None and regras_idx < env_idx:
        _add_alias(aliases, seen, "regras env")
        _add_alias(
            aliases,
            seen,
            " ".join(phrase_tokens[regras_idx : migrations_idx + 1]),
        )

    projeto_idx = _first_index(norms, {"projeto"})
    por_idx = _first_index(norms, {"por"})
    if (
        por_idx is not None
        and projeto_idx is not None
        and migrations_idx < por_idx < projeto_idx
    ):
        _add_alias(aliases, seen, "migrations por projeto")


def _first_index(values: list[str], candidates: set[str]) -> int | None:
    for idx, value in enumerate(values):
        if value in candidates:
            return idx
    return None


def _add_adjacent_sequence(
    aliases: list[str],
    seen: set[str],
    tokens: list[tuple[str, str]],
    sequence: tuple[str, ...],
) -> None:
    norms = [normalized for _display, normalized in tokens]
    for idx in range(0, len(norms) - len(sequence) + 1):
        if tuple(norms[idx : idx + len(sequence)]) != sequence:
            continue
        display = " ".join(token for token, _normalized in tokens[idx : idx + len(sequence)])
        normalized = " ".join(sequence)
        _add_alias(aliases, seen, display)
        _add_alias(aliases, seen, normalized)


def _contains_in_order(values: list[str], sequence: tuple[str, ...]) -> bool:
    cursor = 0
    for value in values:
        if value == sequence[cursor]:
            cursor += 1
            if cursor == len(sequence):
                return True
    return False


def _display_for(tokens: list[tuple[str, str]], normalized_value: str) -> str | None:
    for display, normalized in tokens:
        if normalized == normalized_value:
            return display
    return None


def _add_alias(
    aliases: list[str],
    seen: set[str],
    value: object,
    *,
    force: bool = False,
) -> None:
    alias = _clean_str(value)
    if not alias:
        return
    normalized = normalize_entity_text(alias)
    if not normalized:
        return
    if (
        not force
        and len(normalized.split()) == 1
        and normalized in _GENERIC_SINGLE_ALIASES
    ):
        return
    if alias not in seen:
        seen.add(alias)
        aliases.append(alias)


def _normalized_unique(values: Iterable[object]) -> list[str]:
    return _unique(normalize_entity_text(value) for value in values)


def _unique(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_str(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result
