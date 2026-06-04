import re
import subprocess
import unicodedata
from pathlib import Path


def _slugify(text: str) -> str:
    # Remove acentos/diacríticos antes do regex: \w do Python casa caracteres
    # Unicode (manteria "á"), mas queremos slugs ASCII.
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    slug = re.sub(r"[\s_-]+", "-", text)[:50]
    return slug or "conversa"


def render_markdown(messages: list[dict]) -> str:
    return "\n".join(f"**{m['role']}:** {m['content']}\n" for m in messages)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def write_conversation(
    dest: str | Path,
    conversations_dir: str,
    namespace: str,
    messages: list[dict],
    *,
    timestamp: str,
    author_name: str,
    author_email: str,
    push: bool = False,
    retries: int = 3,
) -> str:
    """Grava a conversa como .md, faz commit (autor brain-bot) e opcionalmente push. Retorna o repo_path relativo."""
    dest = Path(dest)
    first = messages[0]["content"] if messages else "conversa"
    rel = f"{conversations_dir}/{namespace}/{timestamp}-{_slugify(first)}.md"
    path = dest / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(messages), encoding="utf-8")

    _git(["add", rel], dest)
    _git(
        [
            "-c", f"user.name={author_name}",
            "-c", f"user.email={author_email}",
            "commit", "-m", f"chat: {namespace} {timestamp}",
        ],
        dest,
    )
    if push:
        _push_with_retry(dest, retries)
    return rel


def _push_with_retry(dest: Path, retries: int) -> None:
    last_error = None
    for _ in range(retries):
        try:
            _git(["push"], dest)
            return
        except subprocess.CalledProcessError as e:  # non-fast-forward etc.
            last_error = e
            _git(["pull", "--rebase"], dest)
    raise RuntimeError(f"push falhou após {retries} tentativas: {last_error}")
