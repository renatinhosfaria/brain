import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain.queue.base import Job, JobQueue


class PostgresJobQueue(JobQueue):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def enqueue(self, job_type: str, payload: dict) -> uuid.UUID:
        job_id = uuid.uuid4()
        async with self._sf() as s:
            await s.execute(
                text(
                    "INSERT INTO ingestion_jobs (id, type, payload, status, attempts) "
                    "VALUES (:id, :type, CAST(:payload AS jsonb), 'pending', 0)"
                ),
                {"id": job_id, "type": job_type, "payload": json.dumps(payload)},
            )
            await s.commit()
        return job_id

    async def claim_next(self, worker_id: str) -> Job | None:
        async with self._sf() as s:
            row = (
                await s.execute(
                    text(
                        "UPDATE ingestion_jobs SET status='running', locked_by=:w, "
                        "locked_at=now(), run_after=NULL, attempts=attempts+1 "
                        "WHERE id = ("
                        "  SELECT id FROM ingestion_jobs WHERE status='pending' "
                        "  AND (run_after IS NULL OR run_after <= now()) "
                        "  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
                        ") RETURNING id, type, payload, attempts"
                    ),
                    {"w": worker_id},
                )
            ).mappings().first()
            await s.commit()
        if row is None:
            return None
        payload = row["payload"]
        if isinstance(payload, str):  # asyncpg devolve jsonb como str
            payload = json.loads(payload)
        return Job(id=row["id"], type=row["type"], payload=payload, attempts=row["attempts"])

    async def complete(self, job_id: uuid.UUID) -> None:
        async with self._sf() as s:
            await s.execute(
                text(
                    "UPDATE ingestion_jobs SET status='done', locked_by=NULL, "
                    "locked_at=NULL, run_after=NULL WHERE id=:id"
                ),
                {"id": job_id},
            )
            await s.commit()

    async def fail(self, job_id: uuid.UUID, error: str, *, max_attempts: int) -> None:
        async with self._sf() as s:
            await s.execute(
                text(
                    "UPDATE ingestion_jobs SET "
                    "status = CASE WHEN attempts >= :m THEN 'failed' ELSE 'pending' END, "
                    "last_error=:e, locked_by=NULL, locked_at=NULL, "
                    "run_after = CASE "
                    "  WHEN attempts >= :m THEN NULL "
                    "  ELSE now() + make_interval(secs => LEAST(300, CAST(power(2, attempts) AS integer))) "
                    "END "
                    "WHERE id=:id"
                ),
                {"id": job_id, "e": error, "m": max_attempts},
            )
            await s.commit()

    async def release_stale(self, older_than_seconds: int) -> int:
        async with self._sf() as s:
            res = await s.execute(
                text(
                    "UPDATE ingestion_jobs SET status='pending', locked_by=NULL, "
                    "locked_at=NULL, run_after=NULL "
                    "WHERE status='running' AND locked_at < now() - make_interval(secs => :sec)"
                ),
                {"sec": older_than_seconds},
            )
            await s.commit()
            return res.rowcount
