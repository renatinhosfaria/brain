import enum
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass


class JobType(enum.StrEnum):
    INDEX_DOCUMENT = "index_document"
    DELETE_DOCUMENT = "delete_document"
    REINDEX = "reindex"


@dataclass
class Job:
    id: uuid.UUID
    type: str
    payload: dict
    attempts: int


class JobQueue(ABC):
    @abstractmethod
    async def enqueue(self, job_type: str, payload: dict) -> uuid.UUID: ...

    @abstractmethod
    async def claim_next(self, worker_id: str) -> Job | None: ...

    @abstractmethod
    async def complete(self, job_id: uuid.UUID) -> None: ...

    @abstractmethod
    async def fail(self, job_id: uuid.UUID, error: str, *, max_attempts: int) -> None: ...

    @abstractmethod
    async def release_stale(self, older_than_seconds: int) -> int: ...
