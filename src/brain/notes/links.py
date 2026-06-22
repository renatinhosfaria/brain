import re

_OBSIDIAN_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def extract_obsidian_links(markdown: str) -> list[dict]:
    links = []
    for match in _OBSIDIAN_LINK_RE.finditer(markdown):
        body = match.group(1)
        target_part, alias = _split_once(body, "|")
        target, anchor = _split_once(target_part, "#")
        target = target.strip()
        if not target:
            continue
        alias = _normalize_optional(alias)
        anchor = _normalize_optional(anchor)
        links.append(
            {
                "target": target,
                "alias": alias,
                "anchor": anchor,
                "raw": match.group(0),
            }
        )
    return links


def _split_once(value: str, separator: str) -> tuple[str, str | None]:
    if separator not in value:
        return value, None
    left, right = value.split(separator, 1)
    return left, right


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
