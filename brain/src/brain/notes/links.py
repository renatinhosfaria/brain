import re


_OBSIDIAN_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def extract_obsidian_links(markdown: str) -> list[dict]:
    links = []
    for match in _OBSIDIAN_LINK_RE.finditer(markdown):
        body = match.group(1)
        target_part, alias = _split_once(body, "|")
        target, anchor = _split_once(target_part, "#")
        links.append(
            {
                "target": target,
                "alias": alias or None,
                "anchor": anchor or None,
                "raw": match.group(0),
            }
        )
    return links


def _split_once(value: str, separator: str) -> tuple[str, str | None]:
    if separator not in value:
        return value, None
    left, right = value.split(separator, 1)
    return left, right
