import asyncio

import structlog

from brain.config import get_settings
from brain.indexing.embeddings import Embedder
from brain.extraction.llm import LLMClient
from brain.ingestion import pipeline
from brain import outbox
from brain.queue.postgres_queue import PostgresJobQueue
from brain.repo_paths import normalize_repo_path
from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory

log = structlog.get_logger()


async def handle_job(session, embedder, llm, settings, job) -> None:
    p = job.payload
    if job.type in ("index_document", "reindex"):
        repo_path, document_path = normalize_repo_path(
            settings.repo_cache_path, p["repo_path"], require_markdown=True
        )
        content = document_path.read_text(encoding="utf-8")
        await pipeline.index_document(
            session, embedder, llm, settings,
            namespace=p["namespace"], repo_path=repo_path,
            content=content, commit_sha=p.get("commit_sha"),
        )
    elif job.type == "delete_document":
        repo_path, _ = normalize_repo_path(
            settings.repo_cache_path, p["repo_path"], require_markdown=False
        )
        await repo.delete_document_by_path(session, repo_path)
        await session.commit()
    elif job.type == "extract_facts":
        await pipeline.extract_and_store_facts(
            session, embedder, llm, namespace=p["namespace"], messages=p["messages"]
        )
    else:
        raise ValueError(f"tipo de job desconhecido: {job.type}")


async def run_once(
    session_factory,
    queue,
    embedder,
    llm,
    settings,
    worker_id="worker",
    outbox_client=None,
) -> bool:
    job = await queue.claim_next(worker_id)
    if job is None:
        return await outbox.deliver_once(
            session_factory,
            settings,
            worker_id=worker_id,
            client=outbox_client,
        )
    try:
        async with session_factory() as session:
            await handle_job(session, embedder, llm, settings, job)
        await queue.complete(job.id)
        log.info("job_done", job_id=str(job.id), type=job.type)
    except Exception as e:  # noqa: BLE001
        log.error("job_failed", job_id=str(job.id), type=job.type, error=str(e))
        await queue.fail(job.id, str(e), max_attempts=settings.max_job_attempts)
    return True


async def run_forever() -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    sf = make_session_factory(engine)
    queue = PostgresJobQueue(sf)
    embedder = Embedder.from_settings(settings)
    llm = LLMClient.from_settings(settings)
    idle_loops = 0
    while True:
        did_work = await run_once(sf, queue, embedder, llm, settings)
        if did_work:
            idle_loops = 0
        else:
            idle_loops += 1
            if idle_loops % 12 == 0:  # ~a cada 60s ocioso
                await queue.release_stale(settings.job_stale_seconds)
            await asyncio.sleep(5)


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
