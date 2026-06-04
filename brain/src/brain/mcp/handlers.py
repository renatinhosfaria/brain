import datetime as dt
import uuid
from dataclasses import dataclass

from brain.graph import age
from brain.ingestion import git_writer
from brain.queue.base import JobType
from brain.search.retriever import search as _search
from brain.storage import repositories as repo


@dataclass
class Deps:
    session_factory: object
    embedder: object
    llm: object
    queue: object
    settings: object


def _now_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")


def _mem_dict(m) -> dict | None:
    if m is None:
        return None
    return {
        "id": str(m.id),
        "namespace": m.namespace,
        "content": m.content,
        "confidence": m.confidence,
        "source": m.source,
        "metadata": m.meta,
    }


def _doc_dict(d) -> dict | None:
    if d is None:
        return None
    return {
        "id": str(d.id),
        "namespace": d.namespace,
        "repo_path": d.repo_path,
        "title": d.title,
        "content": d.raw_content,
    }


# ---------- Memória & recall ----------
async def remember(deps: Deps, namespace: str, messages: list[dict], metadata: dict | None = None) -> dict:
    s = deps.settings
    rel = git_writer.write_conversation(
        s.repo_cache_path, s.conversations_dir, namespace, messages,
        timestamp=_now_stamp(), author_name=s.git_author_name,
        author_email=s.git_author_email, push=s.git_push_enabled,
    )
    job_facts = await deps.queue.enqueue(
        JobType.EXTRACT_FACTS.value, {"namespace": namespace, "messages": messages}
    )
    job_index = await deps.queue.enqueue(
        JobType.INDEX_DOCUMENT.value, {"namespace": namespace, "repo_path": rel}
    )
    return {"note_path": rel, "job_ids": [str(job_facts), str(job_index)]}


async def search(deps: Deps, query: str, namespace: str | None = None,
                 limit: int = 10, include_graph: bool = False) -> dict:
    async with deps.session_factory() as s:
        return await _search(s, deps.embedder, query, namespace=namespace,
                             limit=limit, include_graph=include_graph)


async def get_memory(deps: Deps, id: str) -> dict | None:
    async with deps.session_factory() as s:
        return _mem_dict(await repo.get_memory(s, uuid.UUID(id)))


async def list_memories(deps: Deps, namespace: str | None = None) -> list[dict]:
    async with deps.session_factory() as s:
        return [_mem_dict(m) for m in await repo.list_memories(s, namespace)]


async def update_memory(deps: Deps, id: str, content: str | None = None) -> dict | None:
    async with deps.session_factory() as s:
        embedding = None
        if content is not None:
            (embedding,) = await deps.embedder.embed([content])
        m = await repo.update_memory(s, uuid.UUID(id), content=content, embedding=embedding)
        await s.commit()
        return _mem_dict(m)


async def move_memory(deps: Deps, id: str, namespace: str) -> dict | None:
    async with deps.session_factory() as s:
        m = await repo.move_memory(s, uuid.UUID(id), namespace)
        await s.commit()
        return _mem_dict(m)


async def delete_memory(deps: Deps, id: str) -> dict:
    async with deps.session_factory() as s:
        ok = await repo.delete_memory(s, uuid.UUID(id))
        await s.commit()
        return {"deleted": ok}


async def merge_memories(deps: Deps, ids: list[str], into: str | None = None) -> dict:
    async with deps.session_factory() as s:
        target = await repo.merge_memories(
            s, [uuid.UUID(i) for i in ids], uuid.UUID(into) if into else None
        )
        await s.commit()
        return {"into": str(target)}


# ---------- Documentos ----------
async def get_document(deps: Deps, id_or_path: str) -> dict | None:
    async with deps.session_factory() as s:
        try:
            doc = await repo.get_document(s, id=uuid.UUID(id_or_path))
        except ValueError:
            doc = await repo.get_document(s, repo_path=id_or_path)
        return _doc_dict(doc)


async def list_documents(deps: Deps, namespace: str | None = None) -> list[dict]:
    async with deps.session_factory() as s:
        return [_doc_dict(d) for d in await repo.list_documents(s, namespace)]


async def reindex(deps: Deps, repo_path: str, namespace: str) -> dict:
    job_id = await deps.queue.enqueue(
        JobType.REINDEX.value, {"namespace": namespace, "repo_path": repo_path}
    )
    return {"job_id": str(job_id)}


# ---------- Grafo ----------
async def get_entity(deps: Deps, name: str, namespace: str) -> dict | None:
    async with deps.session_factory() as s:
        return await age.get_entity(s, name, namespace)


async def search_entities(deps: Deps, query: str, namespace: str) -> list[dict]:
    async with deps.session_factory() as s:
        return await age.search_entities(s, query, namespace)


async def get_related(deps: Deps, entity: str, namespace: str, depth: int = 1) -> list[dict]:
    async with deps.session_factory() as s:
        return await age.get_related(s, entity, namespace, depth)


async def update_entity(deps: Deps, name: str, namespace: str, props: dict) -> dict:
    async with deps.session_factory() as s:
        await age.update_entity(s, name, namespace, props)
        return {"updated": True}


async def merge_entities(deps: Deps, sources: list[str], into: str, namespace: str) -> dict:
    async with deps.session_factory() as s:
        await age.merge_entities(s, sources, into, namespace)
        return {"into": into}


async def delete_entity(deps: Deps, name: str, namespace: str) -> dict:
    async with deps.session_factory() as s:
        await age.delete_entity(s, name, namespace)
        return {"deleted": True}


# ---------- Namespaces ----------
async def create_namespace(deps: Deps, name: str, description: str | None = None) -> dict:
    async with deps.session_factory() as s:
        ns = await repo.create_namespace(s, name, description)
        await s.commit()
        return {"name": ns.name, "description": ns.description}


async def list_namespaces(deps: Deps) -> list[dict]:
    async with deps.session_factory() as s:
        return [{"name": n.name, "description": n.description} for n in await repo.list_namespaces(s)]
