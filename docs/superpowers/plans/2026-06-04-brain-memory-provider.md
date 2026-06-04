# brain — Provedor de Memória: Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir o `brain`, um provedor de memória pessoal exposto como servidor MCP, com ingestão de documentos via repo GitHub, extração de fatos de conversas via OpenAI, busca semântica unificada e grafo de entidades em Postgres (pgvector + Apache AGE).

**Architecture:** Monolito modular em Python empacotado em uma única imagem Docker que sobe em dois papéis (`api` FastAPI+MCP e `worker` de ingestão), ao lado de um Postgres custom com pgvector e Apache AGE. A comunicação entre API e worker é uma fila durável em tabela Postgres (`SELECT ... FOR UPDATE SKIP LOCKED`) atrás de uma interface `JobQueue` abstrata.

**Tech Stack:** Python 3.12, `uv`, SQLAlchemy 2.0 async (asyncpg), Alembic, pgvector, Apache AGE (Cypher via SQL), MCP SDK (FastMCP / streamable HTTP), FastAPI, OpenAI SDK, pydantic-settings, structlog, tenacity, pytest + pytest-asyncio + testcontainers.

**Spec de referência:** [docs/superpowers/specs/2026-06-03-brain-memory-provider-design.md](../specs/2026-06-03-brain-memory-provider-design.md)

---

## Convenções de execução

- Todo o projeto vive na pasta `brain/` na raiz do repositório. **Todos os comandos abaixo rodam de dentro de `brain/`** salvo indicação contrária.
- Gerenciador de dependências: **`uv`**. Rodar testes: `uv run pytest`. Adicionar deps: `uv add <pkg>` / `uv add --dev <pkg>`.
- Cada teste de integração que toca o banco usa **testcontainers** subindo a imagem custom do projeto (`brain-postgres:local`, construída na Task 2). Docker precisa estar disponível na máquina de desenvolvimento.
- TDD obrigatório: escrever o teste que falha, ver falhar, implementar o mínimo, ver passar, commitar.
- Mensagens de commit em português, prefixo Conventional Commits.

---

## Estrutura de arquivos (mapa de responsabilidades)

| Arquivo | Responsabilidade |
|---|---|
| `brain/pyproject.toml` | Metadados do pacote, deps, config de pytest |
| `brain/src/brain/config.py` | `Settings` (pydantic-settings) + `get_settings()` |
| `brain/src/brain/auth.py` | Validação de bearer token |
| `brain/src/brain/storage/db.py` | Engine async, session factory, hook de carga do AGE por conexão |
| `brain/src/brain/storage/models.py` | Modelos SQLAlchemy: `Document`, `Chunk`, `Memory`, `Namespace`, `IngestionJob` |
| `brain/src/brain/storage/repositories.py` | CRUD + busca vetorial para documents/chunks/memories/namespaces |
| `brain/src/brain/queue/base.py` | `Job` (dataclass), `JobType`, interface abstrata `JobQueue` |
| `brain/src/brain/queue/postgres_queue.py` | `PostgresJobQueue` (SKIP LOCKED, retry, dead-letter) |
| `brain/src/brain/indexing/chunker.py` | `chunk_markdown()` — split por headings + overlap |
| `brain/src/brain/indexing/embeddings.py` | `Embedder` — wrapper OpenAI embeddings |
| `brain/src/brain/extraction/llm.py` | `LLMClient.complete_json()` — chat OpenAI com saída JSON validada |
| `brain/src/brain/extraction/facts.py` | `extract_facts()` — fatos de mensagens |
| `brain/src/brain/extraction/entities.py` | `extract_entities()` — entidades/relações de texto |
| `brain/src/brain/graph/age.py` | Operações de grafo no Apache AGE (upsert/get_related/merge/delete/search) |
| `brain/src/brain/search/retriever.py` | Busca unificada vetorial + expansão por grafo + ranking |
| `brain/src/brain/ingestion/git_sync.py` | clone/pull + diff de arquivos alterados |
| `brain/src/brain/ingestion/git_writer.py` | Escreve `.md` de conversa, commit (`brain-bot`), push com retry |
| `brain/src/brain/ingestion/pipeline.py` | Orquestra `index_document` e `extract_facts` |
| `brain/src/brain/worker.py` | Loop do worker: claim → processa → complete/fail |
| `brain/src/brain/mcp/server.py` | Servidor FastMCP + registro de todas as tools |
| `brain/src/brain/main.py` | FastAPI: monta MCP, webhook GitHub, `/health`, `/status` |
| `brain/docker/postgres/Dockerfile` | Imagem Postgres custom (pgvector + AGE) |
| `brain/Dockerfile` | Imagem da app (api + worker) |
| `brain/docker-compose.yml` | Orquestra api · worker · postgres |
| `brain/migrations/` | Migrations Alembic |

---

## Task 1: Scaffold do projeto

**Files:**
- Create: `brain/pyproject.toml`
- Create: `brain/src/brain/__init__.py`
- Create: `brain/tests/__init__.py`
- Create: `brain/tests/test_smoke.py`
- Create: `brain/.gitignore`

- [ ] **Step 1: Criar `pyproject.toml`**

```toml
[project]
name = "brain"
version = "0.1.0"
description = "Provedor de memória pessoal exposto como servidor MCP"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "mcp>=1.2",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.30",
    "alembic>=1.14",
    "pgvector>=0.3",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "openai>=1.55",
    "structlog>=24.4",
    "tenacity>=9.0",
    "tiktoken>=0.8",
    "httpx>=0.27",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "testcontainers[postgres]>=4.8",
    "anyio>=4.6",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/brain"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 2: Criar `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
.env
.pytest_cache/
*.egg-info/
.ruff_cache/
repo_cache/
```

- [ ] **Step 3: Criar `src/brain/__init__.py` e `tests/__init__.py` (vazios)**

Ambos arquivos vazios.

- [ ] **Step 4: Escrever o smoke test**

`brain/tests/test_smoke.py`:
```python
import brain


def test_package_importavel():
    assert brain is not None
```

- [ ] **Step 5: Instalar deps e rodar o teste**

Run: `cd brain && uv sync && uv run pytest tests/test_smoke.py -v`
Expected: PASS (1 passed)

- [ ] **Step 6: Commit**

```bash
git add brain/pyproject.toml brain/.gitignore brain/src brain/tests
git commit -m "chore(brain): scaffold do projeto Python com uv e pytest"
```

---

## Task 2: Imagem Postgres custom (pgvector + Apache AGE)

> **Maior risco técnico do projeto (spec §11.1). Validamos primeiro.** Multi-stage build: um estágio builder compila o Apache AGE numa **release fixada** (`release/PG16/1.5.0`) e a imagem final parte de `pgvector/pgvector:pg16` (já traz pgvector) copiando apenas os artefatos do AGE. Reprodutível e enxuto.

**Files:**
- Create: `brain/docker/postgres/Dockerfile`
- Create: `brain/docker/postgres/init/01-extensions.sql`
- Create: `brain/tests/integration/__init__.py`
- Create: `brain/tests/integration/conftest.py`
- Create: `brain/tests/integration/test_postgres_image.py`

- [ ] **Step 1: Escrever o Dockerfile do Postgres**

`brain/docker/postgres/Dockerfile`:
```dockerfile
# ---- Estágio builder: compila o Apache AGE numa release fixa ----
FROM pgvector/pgvector:pg16 AS builder

ARG AGE_REF=release/PG16/1.5.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        postgresql-server-dev-16 \
        flex \
        bison \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/apache/age.git /tmp/age \
    && cd /tmp/age \
    && git checkout ${AGE_REF} \
    && make \
    && make install

# ---- Imagem final: pgvector + artefatos do AGE copiados ----
FROM pgvector/pgvector:pg16

# Copia a lib e os arquivos de extensão do AGE compilados no builder
COPY --from=builder /usr/lib/postgresql/16/lib/age.so /usr/lib/postgresql/16/lib/age.so
COPY --from=builder /usr/share/postgresql/16/extension/age.control /usr/share/postgresql/16/extension/age.control
COPY --from=builder /usr/share/postgresql/16/extension/age--*.sql /usr/share/postgresql/16/extension/

# Scripts de init rodam na primeira inicialização do cluster
COPY init/01-extensions.sql /docker-entrypoint-initdb.d/01-extensions.sql
```

- [ ] **Step 2: Escrever o script de init das extensões**

`brain/docker/postgres/init/01-extensions.sql`:
```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('brain');
```

- [ ] **Step 3: Buildar a imagem localmente**

Run: `docker build -t brain-postgres:local brain/docker/postgres`
Expected: build conclui com sucesso (pode levar alguns minutos compilando o AGE).

- [ ] **Step 4: Escrever a fixture de container compartilhada**

`brain/tests/integration/conftest.py`:
```python
import pytest
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def pg_container():
    container = (
        PostgresContainer(
            image="brain-postgres:local",
            username="brain",
            password="brain",
            dbname="brain",
        )
        .with_command("postgres -c shared_preload_libraries=age")
    )
    with container as pg:
        yield pg


@pytest.fixture(scope="session")
def sync_dsn(pg_container):
    # DSN psycopg2 para asserts diretos em testes de infra
    return pg_container.get_connection_url()


@pytest.fixture(scope="session")
def async_dsn(pg_container):
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    return f"postgresql+asyncpg://brain:brain@{host}:{port}/brain"
```

`brain/tests/integration/__init__.py`: vazio.

- [ ] **Step 5: Escrever o teste de validação da imagem**

`brain/tests/integration/test_postgres_image.py`:
```python
import sqlalchemy as sa
from sqlalchemy import text


def test_extensoes_vector_e_age_presentes(sync_dsn):
    engine = sa.create_engine(sync_dsn)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'age')")
        ).scalars().all()
    assert set(rows) == {"vector", "age"}


def test_grafo_brain_existe(sync_dsn):
    engine = sa.create_engine(sync_dsn)
    with engine.connect() as conn:
        conn.execute(text("LOAD 'age'"))
        conn.execute(text('SET search_path = ag_catalog, "$user", public'))
        names = conn.execute(text("SELECT name::text FROM ag_graph")).scalars().all()
    assert "brain" in names


def test_vector_aceita_2000_dims(sync_dsn):
    engine = sa.create_engine(sync_dsn)
    with engine.connect() as conn:
        conn.execute(text("CREATE TEMP TABLE t (v vector(2000))"))
        conn.execute(text("INSERT INTO t (v) VALUES (:v)"), {"v": "[" + ",".join(["0"] * 2000) + "]"})
        n = conn.execute(text("SELECT count(*) FROM t")).scalar()
    assert n == 1
```

- [ ] **Step 6: Rodar os testes de integração**

Run: `cd brain && uv run pytest tests/integration/test_postgres_image.py -v`
Expected: PASS (3 passed). Confirma que pgvector e AGE coexistem e que `vector(2000)` funciona.

- [ ] **Step 7: Commit**

```bash
git add brain/docker brain/tests/integration
git commit -m "feat(brain): imagem Postgres custom com pgvector + Apache AGE validada"
```

---

## Task 3: Configuração (`Settings`)

**Files:**
- Create: `brain/src/brain/config.py`
- Create: `brain/tests/test_config.py`

- [ ] **Step 1: Escrever o teste de config**

`brain/tests/test_config.py`:
```python
from brain.config import Settings


def test_settings_le_variaveis_de_ambiente(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("BRAIN_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("WEBHOOK_SECRET", "hmac-secret")
    monkeypatch.setenv("REPO_URL", "https://github.com/user/brain-vault.git")

    s = Settings()

    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.openai_api_key == "sk-test"
    assert s.embedding_model == "text-embedding-3-large"
    assert s.embedding_dim == 2000
    assert s.max_job_attempts == 5
    assert s.conversations_dir == "conversas"
    assert s.git_author_name == "brain-bot"


def test_defaults_de_modelos():
    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        openai_api_key="sk",
        github_token="gh",
        brain_auth_token="t",
        webhook_secret="w",
        repo_url="https://x/y.git",
    )
    assert s.extraction_model == "gpt-4o-mini"
    assert s.chunk_max_tokens == 512
    assert s.chunk_overlap_tokens == 64
```

- [ ] **Step 2: Rodar para ver falhar**

Run: `cd brain && uv run pytest tests/test_config.py -v`
Expected: FAIL (ModuleNotFoundError: brain.config)

- [ ] **Step 3: Implementar `config.py`**

`brain/src/brain/config.py`:
```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Infra
    database_url: str
    openai_api_key: str
    github_token: str
    brain_auth_token: str
    webhook_secret: str
    repo_url: str

    # Git
    repo_cache_path: str = "repo_cache"
    conversations_dir: str = "conversas"
    git_author_name: str = "brain-bot"
    git_author_email: str = "brain-bot@users.noreply.github.com"

    # IA
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 2000
    extraction_model: str = "gpt-4o-mini"

    # Indexação
    chunk_max_tokens: int = 512
    chunk_overlap_tokens: int = 64

    # Fila
    max_job_attempts: int = 5
    job_stale_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Rodar para ver passar**

Run: `cd brain && uv run pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/src/brain/config.py brain/tests/test_config.py
git commit -m "feat(brain): configuração via pydantic-settings"
```

---

## Task 4: Auth (bearer token)

**Files:**
- Create: `brain/src/brain/auth.py`
- Create: `brain/tests/test_auth.py`

- [ ] **Step 1: Escrever o teste**

`brain/tests/test_auth.py`:
```python
import pytest

from brain.auth import verify_bearer_token, AuthError


def test_token_valido_passa():
    verify_bearer_token("Bearer secret", expected="secret")  # não levanta


def test_token_invalido_falha():
    with pytest.raises(AuthError):
        verify_bearer_token("Bearer errado", expected="secret")


def test_header_ausente_falha():
    with pytest.raises(AuthError):
        verify_bearer_token(None, expected="secret")


def test_formato_sem_bearer_falha():
    with pytest.raises(AuthError):
        verify_bearer_token("secret", expected="secret")
```

- [ ] **Step 2: Rodar para ver falhar**

Run: `cd brain && uv run pytest tests/test_auth.py -v`
Expected: FAIL (ModuleNotFoundError: brain.auth)

- [ ] **Step 3: Implementar `auth.py`**

`brain/src/brain/auth.py`:
```python
import hmac


class AuthError(Exception):
    """Token de autenticação ausente ou inválido."""


def verify_bearer_token(authorization_header: str | None, *, expected: str) -> None:
    if not authorization_header:
        raise AuthError("Authorization header ausente")
    prefix = "Bearer "
    if not authorization_header.startswith(prefix):
        raise AuthError("Formato esperado: 'Bearer <token>'")
    token = authorization_header[len(prefix):]
    # Comparação em tempo constante para evitar timing attacks
    if not hmac.compare_digest(token, expected):
        raise AuthError("Token inválido")
```

- [ ] **Step 4: Rodar para ver passar**

Run: `cd brain && uv run pytest tests/test_auth.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/src/brain/auth.py brain/tests/test_auth.py
git commit -m "feat(brain): autenticação por bearer token"
```

---

> **NOTA DE DESIGN (embeddings 2000 dims + HNSW):** O pgvector só permite índices ANN (HNSW/IVFFlat) em vetores de **até 2000 dimensões**. Por isso usamos `text-embedding-3-large` com `dimensions=2000` — o modelo é treinado com Matryoshka, então truncar para 2000 tem perda de qualidade negligenciável. Com isso habilitamos índice **HNSW** (`vector_cosine_ops`) nas colunas de embedding de `chunks` e `memories`, deixando a busca vetorial rápida e escalável. O `Embedder` passa `dimensions` à API (Task 9) e a migration cria os índices HNSW (Task 6).

---

## Task 5: Camada de storage — engine, session e modelos

**Files:**
- Create: `brain/src/brain/storage/__init__.py`
- Create: `brain/src/brain/storage/db.py`
- Create: `brain/src/brain/storage/models.py`
- Create: `brain/tests/integration/test_models.py`

- [ ] **Step 1: Criar `storage/__init__.py` (vazio) e implementar `db.py`**

`brain/src/brain/storage/db.py`:
```python
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from pgvector.asyncpg import register_vector


def make_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, pool_pre_ping=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _register_vector(dbapi_connection, _record):  # noqa: ANN001
        # Registra o codec do tipo vector por conexão asyncpg
        dbapi_connection.run_async(register_vector)

    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
```

- [ ] **Step 2: Implementar `models.py`**

`brain/src/brain/storage/models.py`:
```python
import datetime as dt
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBED_DIM = 2000


class Base(DeclarativeBase):
    pass


class Namespace(Base):
    __tablename__ = "namespaces"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    namespace: Mapped[str] = mapped_column(String, index=True)
    repo_path: Mapped[str] = mapped_column(String, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String, index=True)
    commit_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    token_count: Mapped[int] = mapped_column(Integer)

    document: Mapped["Document"] = relationship(back_populates="chunks")


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    namespace: Mapped[str] = mapped_column(String, index=True)
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String, default="fact")
    source: Mapped[str] = mapped_column(String, default="conversation")
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id"), nullable=True
    )
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

- [ ] **Step 3: Escrever o teste de integração dos modelos**

`brain/tests/integration/test_models.py`:
```python
import pytest_asyncio
from sqlalchemy import select

from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base, Chunk, Document, Namespace


@pytest_asyncio.fixture
async def session(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_inserir_documento_e_chunk_com_embedding(session):
    session.add(Namespace(name="trabalho"))
    doc = Document(
        namespace="trabalho", repo_path="notas/a.md", raw_content="oi mundo", content_hash="h1"
    )
    session.add(doc)
    await session.flush()
    session.add(
        Chunk(document_id=doc.id, ordinal=0, text="oi mundo", embedding=[0.1] * 2000, token_count=2)
    )
    await session.commit()

    chunks = (await session.execute(select(Chunk))).scalars().all()
    assert len(chunks) == 1
    assert len(chunks[0].embedding) == 2000
```

- [ ] **Step 4: Rodar o teste**

Run: `cd brain && uv run pytest tests/integration/test_models.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/src/brain/storage brain/tests/integration/test_models.py
git commit -m "feat(brain): engine async, session factory e modelos SQLAlchemy"
```

---

## Task 6: Migrations Alembic

**Files:**
- Create: `brain/alembic.ini`
- Create: `brain/migrations/env.py`
- Create: `brain/migrations/script.py.mako`
- Create: `brain/migrations/versions/0001_inicial.py`
- Create: `brain/tests/integration/test_migrations.py`

- [ ] **Step 1: Criar `alembic.ini`**

`brain/alembic.ini`:
```ini
[alembic]
script_location = migrations
prepend_sys_path = src

[loggers]
keys = root

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

- [ ] **Step 2: Criar `migrations/script.py.mako`**

`brain/migrations/script.py.mako`:
```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 3: Criar `migrations/env.py` (async)**

`brain/migrations/env.py`:
```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from brain.config import get_settings
from brain.storage.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:  # noqa: ANN001
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 4: Escrever a migration inicial**

`brain/migrations/versions/0001_inicial.py`:
```python
"""schema inicial

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBED_DIM = 2000


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS age")

    op.create_table(
        "namespaces",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("repo_path", sa.String(), nullable=False, unique=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("commit_sha", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_documents_namespace", "documents", ["namespace"])
    op.create_index("ix_documents_content_hash", "documents", ["content_hash"])

    op.create_table(
        "chunks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", UUID(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "memories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="fact"),
        sa.Column("source", sa.String(), nullable=False, server_default="conversation"),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("supersedes_id", UUID(as_uuid=True),
                  sa.ForeignKey("memories.id"), nullable=True),
        sa.Column("metadata", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_memories_namespace", "memories", ["namespace"])
    op.execute(
        "CREATE INDEX ix_memories_embedding_hnsw ON memories "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_jobs_status", "ingestion_jobs", ["status"])
    op.create_index("ix_jobs_type", "ingestion_jobs", ["type"])


def downgrade() -> None:
    op.drop_table("ingestion_jobs")
    op.drop_table("memories")
    op.drop_table("chunks")
    op.drop_table("documents")
    op.drop_table("namespaces")
```

- [ ] **Step 5: Escrever teste que aplica a migration num banco limpo**

`brain/tests/integration/test_migrations.py`:
```python
import subprocess

import sqlalchemy as sa
from sqlalchemy import text


def test_alembic_upgrade_cria_tabelas(sync_dsn, async_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("BRAIN_AUTH_TOKEN", "t")
    monkeypatch.setenv("WEBHOOK_SECRET", "w")
    monkeypatch.setenv("REPO_URL", "https://x/y.git")

    # Garante banco limpo das tabelas (a imagem já tem extensões)
    engine = sa.create_engine(sync_dsn)
    with engine.begin() as conn:
        for t in ["ingestion_jobs", "memories", "chunks", "documents", "namespaces", "alembic_version"]:
            conn.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))

    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    with engine.connect() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        ).scalars().all()
    assert {"documents", "chunks", "memories", "ingestion_jobs", "namespaces"} <= set(tables)
```

- [ ] **Step 6: Rodar o teste**

Run: `cd brain && uv run pytest tests/integration/test_migrations.py -v`
Expected: PASS (1 passed)

- [ ] **Step 7: Commit**

```bash
git add brain/alembic.ini brain/migrations brain/tests/integration/test_migrations.py
git commit -m "feat(brain): migrations Alembic async com schema inicial"
```

---

## Task 7: Fila durável (`JobQueue` + `PostgresJobQueue`)

**Files:**
- Create: `brain/src/brain/queue/__init__.py`
- Create: `brain/src/brain/queue/base.py`
- Create: `brain/src/brain/queue/postgres_queue.py`
- Create: `brain/tests/integration/test_queue.py`

- [ ] **Step 1: Criar `queue/__init__.py` (vazio) e implementar `base.py`**

`brain/src/brain/queue/base.py`:
```python
import enum
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass


class JobType(str, enum.Enum):
    INDEX_DOCUMENT = "index_document"
    DELETE_DOCUMENT = "delete_document"
    EXTRACT_FACTS = "extract_facts"
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
```

- [ ] **Step 2: Implementar `postgres_queue.py`**

`brain/src/brain/queue/postgres_queue.py`:
```python
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
                        "locked_at=now(), attempts=attempts+1 "
                        "WHERE id = ("
                        "  SELECT id FROM ingestion_jobs WHERE status='pending' "
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
                    "locked_at=NULL WHERE id=:id"
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
                    "last_error=:e, locked_by=NULL, locked_at=NULL WHERE id=:id"
                ),
                {"id": job_id, "e": error, "m": max_attempts},
            )
            await s.commit()

    async def release_stale(self, older_than_seconds: int) -> int:
        async with self._sf() as s:
            res = await s.execute(
                text(
                    "UPDATE ingestion_jobs SET status='pending', locked_by=NULL, locked_at=NULL "
                    "WHERE status='running' AND locked_at < now() - make_interval(secs => :sec)"
                ),
                {"sec": older_than_seconds},
            )
            await s.commit()
            return res.rowcount
```

- [ ] **Step 3: Escrever os testes de integração da fila**

`brain/tests/integration/test_queue.py`:
```python
import pytest_asyncio
from sqlalchemy import text

from brain.queue.base import JobType
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base


@pytest_asyncio.fixture
async def sf(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


async def test_enqueue_e_claim(sf):
    q = PostgresJobQueue(sf)
    jid = await q.enqueue(JobType.INDEX_DOCUMENT.value, {"repo_path": "a.md"})
    job = await q.claim_next("w1")
    assert job is not None
    assert job.id == jid
    assert job.payload == {"repo_path": "a.md"}
    assert job.attempts == 1


async def test_claim_vazio_retorna_none(sf):
    q = PostgresJobQueue(sf)
    assert await q.claim_next("w1") is None


async def test_skip_locked_nao_entrega_o_mesmo_job(sf):
    q = PostgresJobQueue(sf)
    await q.enqueue(JobType.INDEX_DOCUMENT.value, {"n": 1})
    j1 = await q.claim_next("w1")
    j2 = await q.claim_next("w2")  # só existe 1 job pendente
    assert j1 is not None
    assert j2 is None


async def test_fail_reenfileira_ate_o_limite(sf):
    q = PostgresJobQueue(sf)
    jid = await q.enqueue(JobType.EXTRACT_FACTS.value, {})
    # 1a tentativa
    await q.claim_next("w1")
    await q.fail(jid, "boom", max_attempts=2)
    # volta para pending
    job = await q.claim_next("w1")
    assert job is not None and job.attempts == 2
    await q.fail(jid, "boom2", max_attempts=2)
    # agora atingiu o limite -> failed, não reaparece
    assert await q.claim_next("w1") is None
    async with sf() as s:
        status = (await s.execute(
            text("SELECT status FROM ingestion_jobs WHERE id=:id"), {"id": jid}
        )).scalar_one()
    assert status == "failed"


async def test_complete_marca_done(sf):
    q = PostgresJobQueue(sf)
    jid = await q.enqueue(JobType.REINDEX.value, {})
    await q.claim_next("w1")
    await q.complete(jid)
    async with sf() as s:
        status = (await s.execute(
            text("SELECT status FROM ingestion_jobs WHERE id=:id"), {"id": jid}
        )).scalar_one()
    assert status == "done"
```

- [ ] **Step 4: Rodar os testes**

Run: `cd brain && uv run pytest tests/integration/test_queue.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/src/brain/queue brain/tests/integration/test_queue.py
git commit -m "feat(brain): fila durável Postgres com SKIP LOCKED e dead-letter"
```

---

## Task 8: Chunker de markdown

**Files:**
- Create: `brain/src/brain/indexing/__init__.py`
- Create: `brain/src/brain/indexing/chunker.py`
- Create: `brain/tests/test_chunker.py`

- [ ] **Step 1: Escrever os testes (função pura, sem banco)**

`brain/tests/test_chunker.py`:
```python
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
```

- [ ] **Step 2: Rodar para ver falhar**

Run: `cd brain && uv run pytest tests/test_chunker.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implementar `chunker.py`**

`brain/src/brain/indexing/chunker.py`:
```python
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


def _split_by_tokens(text: str, max_tokens: int, overlap: int) -> list[str]:
    tokens = _enc.encode(text)
    if len(tokens) <= max_tokens:
        return [text]
    pieces = []
    step = max(1, max_tokens - overlap)
    for start in range(0, len(tokens), step):
        window = tokens[start : start + max_tokens]
        pieces.append(_enc.decode(window).strip())
        if start + max_tokens >= len(tokens):
            break
    return pieces


def chunk_markdown(text: str, max_tokens: int = 512, overlap: int = 64) -> list[dict]:
    chunks: list[dict] = []
    ordinal = 0
    for section in _split_by_headings(text):
        for piece in _split_by_tokens(section, max_tokens, overlap):
            chunks.append(
                {"ordinal": ordinal, "text": piece, "token_count": count_tokens(piece)}
            )
            ordinal += 1
    return chunks
```

- [ ] **Step 4: Rodar para ver passar**

Run: `cd brain && uv run pytest tests/test_chunker.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/src/brain/indexing brain/tests/test_chunker.py
git commit -m "feat(brain): chunker de markdown por headings com overlap"
```

---

## Task 9: Embedder (wrapper OpenAI embeddings)

**Files:**
- Create: `brain/src/brain/indexing/embeddings.py`
- Create: `brain/tests/test_embeddings.py`

- [ ] **Step 1: Escrever o teste com cliente fake**

`brain/tests/test_embeddings.py`:
```python
from brain.indexing.embeddings import Embedder


class _FakeEmbeddings:
    async def create(self, *, model, input, dimensions):  # noqa: A002
        class _D:
            def __init__(self, e):
                self.embedding = e

        class _R:
            data = [_D([0.0] * dimensions) for _ in input]

        return _R()


class _FakeClient:
    embeddings = _FakeEmbeddings()


async def test_embed_retorna_um_vetor_por_texto():
    emb = Embedder(client=_FakeClient(), model="text-embedding-3-large", dim=2000)
    vecs = await emb.embed(["a", "b", "c"])
    assert len(vecs) == 3
    assert all(len(v) == 2000 for v in vecs)


async def test_embed_lista_vazia_nao_chama_api():
    emb = Embedder(client=_FakeClient(), model="x", dim=2000)
    assert await emb.embed([]) == []
```

- [ ] **Step 2: Rodar para ver falhar**

Run: `cd brain && uv run pytest tests/test_embeddings.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implementar `embeddings.py`**

`brain/src/brain/indexing/embeddings.py`:
```python
from openai import AsyncOpenAI

from brain.config import Settings


class Embedder:
    def __init__(self, client, model: str, dim: int) -> None:  # noqa: ANN001
        self._client = client
        self._model = model
        self._dim = dim

    @classmethod
    def from_settings(cls, settings: Settings) -> "Embedder":
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        return cls(client, settings.embedding_model, settings.embedding_dim)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._client.embeddings.create(
            model=self._model, input=texts, dimensions=self._dim
        )
        return [item.embedding for item in resp.data]
```

- [ ] **Step 4: Rodar para ver passar**

Run: `cd brain && uv run pytest tests/test_embeddings.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/src/brain/indexing/embeddings.py brain/tests/test_embeddings.py
git commit -m "feat(brain): Embedder wrapper sobre OpenAI embeddings"
```

---

## Task 10: LLMClient (chat com saída JSON)

**Files:**
- Create: `brain/src/brain/extraction/__init__.py`
- Create: `brain/src/brain/extraction/llm.py`
- Create: `brain/tests/test_llm.py`

- [ ] **Step 1: Escrever o teste com cliente fake**

`brain/tests/test_llm.py`:
```python
import json

from brain.extraction.llm import LLMClient


class _FakeChat:
    def __init__(self, payload):
        self._payload = payload

    async def create(self, *, model, messages, response_format):
        class _Msg:
            content = json.dumps(self._payload)

        class _Choice:
            message = _Msg()

        class _R:
            choices = [_Choice()]

        return _R()


class _FakeCompletions:
    def __init__(self, payload):
        self.completions = _FakeChat(payload)


class _FakeClient:
    def __init__(self, payload):
        self.chat = _FakeCompletions(payload)


async def test_complete_json_parseia_resposta():
    client = _FakeClient({"facts": [{"content": "x"}]})
    llm = LLMClient(client=client, model="gpt-4o-mini")
    out = await llm.complete_json("sys", "user")
    assert out == {"facts": [{"content": "x"}]}
```

- [ ] **Step 2: Rodar para ver falhar**

Run: `cd brain && uv run pytest tests/test_llm.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implementar `llm.py`**

`brain/src/brain/extraction/llm.py`:
```python
import json

from openai import AsyncOpenAI

from brain.config import Settings


class LLMClient:
    def __init__(self, client, model: str) -> None:  # noqa: ANN001
        self._client = client
        self._model = model

    @classmethod
    def from_settings(cls, settings: Settings) -> "LLMClient":
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        return cls(client, settings.extraction_model)

    async def complete_json(self, system: str, user: str) -> dict:
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
```

- [ ] **Step 4: Rodar para ver passar**

Run: `cd brain && uv run pytest tests/test_llm.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/src/brain/extraction/__init__.py brain/src/brain/extraction/llm.py brain/tests/test_llm.py
git commit -m "feat(brain): LLMClient com saída JSON estruturada"
```

---

## Task 11: Extração de fatos

**Files:**
- Create: `brain/src/brain/extraction/facts.py`
- Create: `brain/tests/test_facts.py`

- [ ] **Step 1: Escrever o teste com LLM fake**

`brain/tests/test_facts.py`:
```python
from brain.extraction.facts import extract_facts


class _FakeLLM:
    def __init__(self, payload):
        self._payload = payload
        self.last_user = None

    async def complete_json(self, system, user):
        self.last_user = user
        return self._payload


async def test_extrai_fatos_normalizados():
    llm = _FakeLLM({"facts": [
        {"content": "usuário prefere TypeScript", "confidence": 0.9},
        {"content": "", "confidence": 0.5},  # descartado: vazio
        {"content": "mora em Goiânia"},        # confidence default 1.0
    ]})
    msgs = [{"role": "user", "content": "eu prefiro typescript e moro em goiânia"}]
    facts = await extract_facts(llm, msgs)
    assert {"content": "usuário prefere TypeScript", "confidence": 0.9} in facts
    assert {"content": "mora em Goiânia", "confidence": 1.0} in facts
    assert len(facts) == 2  # vazio foi removido
    assert "user:" in llm.last_user  # a conversa foi renderizada no prompt


async def test_sem_fatos_retorna_vazio():
    llm = _FakeLLM({"facts": []})
    assert await extract_facts(llm, [{"role": "user", "content": "oi"}]) == []
```

- [ ] **Step 2: Rodar para ver falhar**

Run: `cd brain && uv run pytest tests/test_facts.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implementar `facts.py`**

`brain/src/brain/extraction/facts.py`:
```python
_SYSTEM = (
    "Você extrai fatos memoráveis e duráveis sobre o usuário e seu contexto a partir "
    "de uma conversa. Retorne JSON no formato "
    '{"facts": [{"content": "<fato conciso na 3a pessoa>", "confidence": <0..1>}]}. '
    "Inclua apenas fatos estáveis e úteis no futuro (preferências, decisões, identidade, "
    "relações). Ignore conversa trivial. Se não houver nada memorável, retorne lista vazia."
)


def _render(messages: list[dict]) -> str:
    return "\n".join(f"{m['role']}: {m['content']}" for m in messages)


async def extract_facts(llm, messages: list[dict]) -> list[dict]:  # noqa: ANN001
    data = await llm.complete_json(_SYSTEM, _render(messages))
    result = []
    for f in data.get("facts", []):
        content = (f.get("content") or "").strip()
        if not content:
            continue
        result.append({"content": content, "confidence": float(f.get("confidence", 1.0))})
    return result
```

- [ ] **Step 4: Rodar para ver passar**

Run: `cd brain && uv run pytest tests/test_facts.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/src/brain/extraction/facts.py brain/tests/test_facts.py
git commit -m "feat(brain): extração de fatos de conversas via LLM"
```

---

## Task 12: Extração de entidades e relações

**Files:**
- Create: `brain/src/brain/extraction/entities.py`
- Create: `brain/tests/test_entities.py`

- [ ] **Step 1: Escrever o teste com LLM fake**

`brain/tests/test_entities.py`:
```python
from brain.extraction.entities import extract_entities


class _FakeLLM:
    def __init__(self, payload):
        self._payload = payload

    async def complete_json(self, system, user):
        return self._payload


async def test_extrai_entidades_e_relacoes_normalizadas():
    llm = _FakeLLM({
        "entities": [
            {"name": "Renato", "type": "pessoa"},
            {"name": "brain", "type": "projeto"},
            {"name": "", "type": "x"},  # descartado
        ],
        "relations": [
            {"source": "Renato", "target": "brain", "type": "works_on"},
            {"source": "Renato", "target": "", "type": "x"},  # descartado
        ],
    })
    out = await extract_entities(llm, "Renato trabalha no brain")
    assert {"name": "Renato", "type": "pessoa"} in out["entities"]
    assert len(out["entities"]) == 2
    assert {"source": "Renato", "target": "brain", "type": "works_on"} in out["relations"]
    assert len(out["relations"]) == 1


async def test_payload_incompleto_vira_listas_vazias():
    llm = _FakeLLM({})
    out = await extract_entities(llm, "texto")
    assert out == {"entities": [], "relations": []}
```

- [ ] **Step 2: Rodar para ver falhar**

Run: `cd brain && uv run pytest tests/test_entities.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implementar `entities.py`**

`brain/src/brain/extraction/entities.py`:
```python
_SYSTEM = (
    "Você extrai entidades e relações de um texto para um grafo de conhecimento. "
    "Retorne JSON no formato "
    '{"entities": [{"name": "<nome>", "type": "<pessoa|projeto|conceito|lugar|org>"}], '
    '"relations": [{"source": "<nome>", "target": "<nome>", "type": "<verbo_curto>"}]}. '
    "Use nomes canônicos e consistentes. Relações devem referenciar entidades listadas."
)


async def extract_entities(llm, text: str) -> dict:  # noqa: ANN001
    data = await llm.complete_json(_SYSTEM, text)
    entities = [
        {"name": e["name"].strip(), "type": (e.get("type") or "conceito").strip()}
        for e in data.get("entities", [])
        if (e.get("name") or "").strip()
    ]
    relations = [
        {
            "source": r["source"].strip(),
            "target": r["target"].strip(),
            "type": (r.get("type") or "related_to").strip(),
        }
        for r in data.get("relations", [])
        if (r.get("source") or "").strip() and (r.get("target") or "").strip()
    ]
    return {"entities": entities, "relations": relations}
```

- [ ] **Step 4: Rodar para ver passar**

Run: `cd brain && uv run pytest tests/test_entities.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/src/brain/extraction/entities.py brain/tests/test_entities.py
git commit -m "feat(brain): extração de entidades e relações via LLM"
```

---

## Task 13: Grafo Apache AGE

> **Decisão de segurança:** o AGE tem suporte limitado a parâmetros bind dentro de `cypher()`. Em vez disso, os valores são interpolados como **literais JSON** (`json.dumps`), que escapa aspas/barras corretamente — seguro contra injeção para strings. Dollar-quote com tag única `$cy$`. Cada operação roda `LOAD 'age'` + `SET search_path` na sessão (o AGE exige isso por conexão).

**Files:**
- Create: `brain/src/brain/graph/__init__.py`
- Create: `brain/src/brain/graph/age.py`
- Create: `brain/tests/integration/test_graph.py`

- [ ] **Step 1: Criar `graph/__init__.py` (vazio) e implementar `age.py`**

`brain/src/brain/graph/age.py`:
```python
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

GRAPH = "brain"


async def _prepare(session: AsyncSession) -> None:
    await session.execute(text("LOAD 'age'"))
    await session.execute(text('SET search_path = ag_catalog, "$user", public'))


def _lit(value: object) -> str:
    """Literal seguro para Cypher via JSON (escapa aspas/barras)."""
    return json.dumps(value, ensure_ascii=False)


def _unwrap(agtype_value: object):
    s = str(agtype_value)
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s


async def ensure_graph(session: AsyncSession) -> None:
    await _prepare(session)
    exists = (await session.execute(text("SELECT 1 FROM ag_graph WHERE name='brain'"))).first()
    if not exists:
        await session.execute(text("SELECT create_graph('brain')"))
    await session.commit()


async def upsert_entity(
    session: AsyncSession, name: str, type: str, namespace: str, props: dict | None = None
) -> None:
    await _prepare(session)
    props_json = json.dumps(props or {}, ensure_ascii=False)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MERGE (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"SET n.type = {_lit(type)}, n.props = {_lit(props_json)} "
        f"RETURN n $cy$) AS (n agtype)"
    )
    await session.execute(text(q))
    await session.commit()


async def upsert_relation(
    session: AsyncSession, source: str, target: str, rel_type: str, namespace: str
) -> None:
    await _prepare(session)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (a:Entity {{name: {_lit(source)}, namespace: {_lit(namespace)}}}), "
        f"(b:Entity {{name: {_lit(target)}, namespace: {_lit(namespace)}}}) "
        f"MERGE (a)-[r:REL {{type: {_lit(rel_type)}}}]->(b) "
        f"RETURN r $cy$) AS (r agtype)"
    )
    await session.execute(text(q))
    await session.commit()


async def get_entity(session: AsyncSession, name: str, namespace: str) -> dict | None:
    await _prepare(session)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"RETURN n.name, n.type, n.props $cy$) AS (name agtype, type agtype, props agtype)"
    )
    row = (await session.execute(text(q))).first()
    if row is None:
        return None
    return {"name": _unwrap(row[0]), "type": _unwrap(row[1]), "props": _unwrap(row[2])}


async def search_entities(session: AsyncSession, query: str, namespace: str) -> list[dict]:
    await _prepare(session)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{namespace: {_lit(namespace)}}}) "
        f"WHERE toLower(n.name) CONTAINS toLower({_lit(query)}) "
        f"RETURN n.name, n.type $cy$) AS (name agtype, type agtype)"
    )
    rows = (await session.execute(text(q))).all()
    return [{"name": _unwrap(n), "type": _unwrap(t)} for n, t in rows]


async def get_related(
    session: AsyncSession, name: str, namespace: str, depth: int = 1
) -> list[dict]:
    await _prepare(session)
    depth = max(1, int(depth))
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (a:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}})"
        f"-[*1..{depth}]-(b:Entity) "
        f"RETURN DISTINCT b.name, b.type $cy$) AS (name agtype, type agtype)"
    )
    rows = (await session.execute(text(q))).all()
    return [{"name": _unwrap(n), "type": _unwrap(t)} for n, t in rows]


async def update_entity(
    session: AsyncSession, name: str, namespace: str, props: dict
) -> None:
    await _prepare(session)
    props_json = json.dumps(props, ensure_ascii=False)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"SET n.props = {_lit(props_json)} RETURN n $cy$) AS (n agtype)"
    )
    await session.execute(text(q))
    await session.commit()


async def delete_entity(session: AsyncSession, name: str, namespace: str) -> None:
    await _prepare(session)
    q = (
        f"SELECT * FROM cypher('brain', $cy$ "
        f"MATCH (n:Entity {{name: {_lit(name)}, namespace: {_lit(namespace)}}}) "
        f"DETACH DELETE n $cy$) AS (v agtype)"
    )
    await session.execute(text(q))
    await session.commit()


async def merge_entities(
    session: AsyncSession, sources: list[str], into: str, namespace: str
) -> None:
    """Move relações dos `sources` para `into` e remove os `sources`."""
    await _prepare(session)
    for src in sources:
        if src == into:
            continue
        # relações de saída
        out_q = (
            f"SELECT * FROM cypher('brain', $cy$ "
            f"MATCH (s:Entity {{name: {_lit(src)}, namespace: {_lit(namespace)}}})-[r:REL]->(o), "
            f"(t:Entity {{name: {_lit(into)}, namespace: {_lit(namespace)}}}) "
            f"MERGE (t)-[:REL {{type: r.type}}]->(o) $cy$) AS (v agtype)"
        )
        # relações de entrada
        in_q = (
            f"SELECT * FROM cypher('brain', $cy$ "
            f"MATCH (o)-[r:REL]->(s:Entity {{name: {_lit(src)}, namespace: {_lit(namespace)}}}), "
            f"(t:Entity {{name: {_lit(into)}, namespace: {_lit(namespace)}}}) "
            f"MERGE (o)-[:REL {{type: r.type}}]->(t) $cy$) AS (v agtype)"
        )
        del_q = (
            f"SELECT * FROM cypher('brain', $cy$ "
            f"MATCH (s:Entity {{name: {_lit(src)}, namespace: {_lit(namespace)}}}) "
            f"DETACH DELETE s $cy$) AS (v agtype)"
        )
        await session.execute(text(out_q))
        await session.execute(text(in_q))
        await session.execute(text(del_q))
    await session.commit()
```

- [ ] **Step 2: Escrever os testes de integração do grafo**

`brain/tests/integration/test_graph.py`:
```python
import pytest_asyncio

from brain.graph import age
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base


@pytest_asyncio.fixture
async def session(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as s:
        await age.ensure_graph(s)
        # limpa o grafo entre execuções
        from sqlalchemy import text
        await age._prepare(s)
        await s.execute(text(
            "SELECT * FROM cypher('brain', $cy$ MATCH (n) DETACH DELETE n $cy$) AS (v agtype)"
        ))
        await s.commit()
        yield s
    await engine.dispose()


async def test_upsert_e_get_entity(session):
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho", {"papel": "dev"})
    got = await age.get_entity(session, "Renato", "trabalho")
    assert got["name"] == "Renato"
    assert got["type"] == "pessoa"


async def test_relacao_e_get_related(session):
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_entity(session, "brain", "projeto", "trabalho")
    await age.upsert_relation(session, "Renato", "brain", "works_on", "trabalho")
    related = await age.get_related(session, "Renato", "trabalho", depth=1)
    assert {"name": "brain", "type": "projeto"} in related


async def test_search_entities(session):
    await age.upsert_entity(session, "Goiânia", "lugar", "pessoal")
    found = await age.search_entities(session, "goi", "pessoal")
    assert any(e["name"] == "Goiânia" for e in found)


async def test_delete_entity(session):
    await age.upsert_entity(session, "Temp", "conceito", "pessoal")
    await age.delete_entity(session, "Temp", "pessoal")
    assert await age.get_entity(session, "Temp", "pessoal") is None


async def test_merge_entities_move_relacoes(session):
    await age.upsert_entity(session, "TS", "conceito", "trabalho")
    await age.upsert_entity(session, "TypeScript", "conceito", "trabalho")
    await age.upsert_entity(session, "Renato", "pessoa", "trabalho")
    await age.upsert_relation(session, "Renato", "TS", "likes", "trabalho")
    await age.merge_entities(session, ["TS"], "TypeScript", "trabalho")
    assert await age.get_entity(session, "TS", "trabalho") is None
    related = await age.get_related(session, "Renato", "trabalho")
    assert any(e["name"] == "TypeScript" for e in related)
```

- [ ] **Step 3: Rodar os testes**

Run: `cd brain && uv run pytest tests/integration/test_graph.py -v`
Expected: PASS (5 passed)

- [ ] **Step 4: Commit**

```bash
git add brain/src/brain/graph brain/tests/integration/test_graph.py
git commit -m "feat(brain): operações de grafo no Apache AGE (Cypher)"
```

---

## Task 14: Repositórios (CRUD + busca vetorial)

> Funções module-level recebendo `session` como primeiro argumento (mesmo estilo de `graph/age.py`). A busca vetorial usa `cosine_distance` do pgvector; `score = 1 - distância`.

**Files:**
- Create: `brain/src/brain/storage/repositories.py`
- Create: `brain/tests/integration/test_repositories.py`

- [ ] **Step 1: Implementar `repositories.py`**

`brain/src/brain/storage/repositories.py`:
```python
import uuid

from sqlalchemy import delete, select

from brain.storage.models import Chunk, Document, Memory, Namespace


# ---------- Documentos ----------
async def upsert_document(
    session,
    *,
    namespace: str,
    repo_path: str,
    title: str | None,
    raw_content: str,
    content_hash: str,
    commit_sha: str | None,
) -> Document:
    doc = (
        await session.execute(select(Document).where(Document.repo_path == repo_path))
    ).scalar_one_or_none()
    if doc is None:
        doc = Document(repo_path=repo_path)
        session.add(doc)
    doc.namespace = namespace
    doc.title = title
    doc.raw_content = raw_content
    doc.content_hash = content_hash
    doc.commit_sha = commit_sha
    await session.flush()
    return doc


async def replace_chunks(
    session, document_id: uuid.UUID, chunks: list[dict], embeddings: list[list[float]]
) -> None:
    await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
    for ch, emb in zip(chunks, embeddings, strict=True):
        session.add(
            Chunk(
                document_id=document_id,
                ordinal=ch["ordinal"],
                text=ch["text"],
                embedding=emb,
                token_count=ch["token_count"],
            )
        )
    await session.flush()


async def get_document(
    session, *, id: uuid.UUID | None = None, repo_path: str | None = None
) -> Document | None:
    stmt = select(Document)
    if id is not None:
        stmt = stmt.where(Document.id == id)
    elif repo_path is not None:
        stmt = stmt.where(Document.repo_path == repo_path)
    else:
        return None
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_documents(session, namespace: str | None = None) -> list[Document]:
    stmt = select(Document)
    if namespace:
        stmt = stmt.where(Document.namespace == namespace)
    return list((await session.execute(stmt.order_by(Document.repo_path))).scalars().all())


async def delete_document_by_path(session, repo_path: str) -> bool:
    doc = await get_document(session, repo_path=repo_path)
    if doc is None:
        return False
    await session.delete(doc)  # cascade remove chunks
    await session.flush()
    return True


# ---------- Memórias ----------
async def add_memory(
    session,
    *,
    namespace: str,
    content: str,
    embedding: list[float],
    confidence: float = 1.0,
    source: str = "conversation",
    meta: dict | None = None,
    supersedes_id: uuid.UUID | None = None,
) -> Memory:
    mem = Memory(
        namespace=namespace,
        content=content,
        embedding=embedding,
        confidence=confidence,
        source=source,
        meta=meta or {},
        supersedes_id=supersedes_id,
    )
    session.add(mem)
    await session.flush()
    return mem


async def get_memory(session, id: uuid.UUID) -> Memory | None:
    return (await session.execute(select(Memory).where(Memory.id == id))).scalar_one_or_none()


async def list_memories(session, namespace: str | None = None) -> list[Memory]:
    stmt = select(Memory)
    if namespace:
        stmt = stmt.where(Memory.namespace == namespace)
    return list((await session.execute(stmt.order_by(Memory.created_at.desc()))).scalars().all())


async def update_memory(
    session, id: uuid.UUID, *, content: str | None = None, embedding: list[float] | None = None
) -> Memory | None:
    mem = await get_memory(session, id)
    if mem is None:
        return None
    if content is not None:
        mem.content = content
    if embedding is not None:
        mem.embedding = embedding
    await session.flush()
    return mem


async def move_memory(session, id: uuid.UUID, namespace: str) -> Memory | None:
    mem = await get_memory(session, id)
    if mem is None:
        return None
    mem.namespace = namespace
    await session.flush()
    return mem


async def delete_memory(session, id: uuid.UUID) -> bool:
    mem = await get_memory(session, id)
    if mem is None:
        return False
    await session.delete(mem)
    await session.flush()
    return True


async def merge_memories(
    session, ids: list[uuid.UUID], into: uuid.UUID | None = None
) -> uuid.UUID:
    target = into or ids[0]
    for mid in ids:
        if mid == target:
            continue
        await delete_memory(session, mid)
    return target


# ---------- Busca vetorial ----------
async def search_chunks(
    session, query_embedding: list[float], namespace: str | None, limit: int
) -> list[dict]:
    dist = Chunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = (
        select(Chunk.text, dist, Document.repo_path, Document.namespace)
        .join(Document, Chunk.document_id == Document.id)
    )
    if namespace:
        stmt = stmt.where(Document.namespace == namespace)
    stmt = stmt.order_by(dist).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "text": r.text,
            "score": 1.0 - float(r.distance),
            "source": "document",
            "ref": r.repo_path,
            "namespace": r.namespace,
        }
        for r in rows
    ]


async def search_memories(
    session, query_embedding: list[float], namespace: str | None, limit: int
) -> list[dict]:
    dist = Memory.embedding.cosine_distance(query_embedding).label("distance")
    stmt = select(Memory.id, Memory.content, dist, Memory.namespace)
    if namespace:
        stmt = stmt.where(Memory.namespace == namespace)
    stmt = stmt.order_by(dist).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "text": r.content,
            "score": 1.0 - float(r.distance),
            "source": "memory",
            "ref": str(r.id),
            "namespace": r.namespace,
        }
        for r in rows
    ]


# ---------- Namespaces ----------
async def create_namespace(session, name: str, description: str | None = None) -> Namespace:
    ns = (
        await session.execute(select(Namespace).where(Namespace.name == name))
    ).scalar_one_or_none()
    if ns is None:
        ns = Namespace(name=name, description=description)
        session.add(ns)
        await session.flush()
    return ns


async def list_namespaces(session) -> list[Namespace]:
    return list((await session.execute(select(Namespace).order_by(Namespace.name))).scalars().all())
```

- [ ] **Step 2: Escrever os testes de integração**

`brain/tests/integration/test_repositories.py`:
```python
import pytest_asyncio

from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base


@pytest_asyncio.fixture
async def session(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


def _vec(seed: float) -> list[float]:
    return [seed] * 2000


async def test_upsert_documento_e_replace_chunks(session):
    doc = await repo.upsert_document(
        session, namespace="t", repo_path="a.md", title="A",
        raw_content="oi", content_hash="h1", commit_sha=None,
    )
    await repo.replace_chunks(
        session, doc.id,
        [{"ordinal": 0, "text": "oi", "token_count": 1}],
        [_vec(0.1)],
    )
    await session.commit()
    # upsert idempotente: novo conteúdo substitui chunks
    doc2 = await repo.upsert_document(
        session, namespace="t", repo_path="a.md", title="A",
        raw_content="tchau", content_hash="h2", commit_sha=None,
    )
    assert doc2.id == doc.id
    await repo.replace_chunks(
        session, doc2.id, [{"ordinal": 0, "text": "tchau", "token_count": 1}], [_vec(0.2)]
    )
    await session.commit()
    docs = await repo.list_documents(session, "t")
    assert len(docs) == 1


async def test_busca_vetorial_retorna_mais_proximo(session):
    doc = await repo.upsert_document(
        session, namespace="t", repo_path="a.md", title=None,
        raw_content="x", content_hash="h", commit_sha=None,
    )
    await repo.replace_chunks(
        session, doc.id,
        [{"ordinal": 0, "text": "perto", "token_count": 1},
         {"ordinal": 1, "text": "longe", "token_count": 1}],
        [_vec(0.10), _vec(0.99)],
    )
    await session.commit()
    results = await repo.search_chunks(session, _vec(0.11), "t", limit=1)
    assert len(results) == 1
    assert results[0]["text"] == "perto"
    assert results[0]["source"] == "document"


async def test_memoria_crud_e_busca(session):
    mem = await repo.add_memory(session, namespace="p", content="gosta de café", embedding=_vec(0.5))
    await session.commit()
    assert (await repo.get_memory(session, mem.id)).content == "gosta de café"
    await repo.update_memory(session, mem.id, content="gosta de chá")
    await session.commit()
    assert (await repo.get_memory(session, mem.id)).content == "gosta de chá"
    res = await repo.search_memories(session, _vec(0.5), "p", limit=5)
    assert res[0]["source"] == "memory"
    assert await repo.delete_memory(session, mem.id) is True


async def test_namespace_idempotente(session):
    await repo.create_namespace(session, "t", "trabalho")
    await repo.create_namespace(session, "t", "trabalho")
    await session.commit()
    names = [n.name for n in await repo.list_namespaces(session)]
    assert names.count("t") == 1
```

- [ ] **Step 3: Rodar os testes**

Run: `cd brain && uv run pytest tests/integration/test_repositories.py -v`
Expected: PASS (4 passed)

- [ ] **Step 4: Commit**

```bash
git add brain/src/brain/storage/repositories.py brain/tests/integration/test_repositories.py
git commit -m "feat(brain): repositórios CRUD e busca vetorial pgvector"
```

---

## Task 15: Busca unificada (retriever)

**Files:**
- Create: `brain/src/brain/search/__init__.py`
- Create: `brain/src/brain/search/retriever.py`
- Create: `brain/tests/integration/test_retriever.py`

- [ ] **Step 1: Criar `search/__init__.py` (vazio) e implementar `retriever.py`**

`brain/src/brain/search/retriever.py`:
```python
from brain.graph import age
from brain.storage import repositories as repo


async def search(
    session,
    embedder,
    query: str,
    *,
    namespace: str | None = None,
    limit: int = 10,
    include_graph: bool = False,
) -> dict:
    (qvec,) = await embedder.embed([query])
    chunk_hits = await repo.search_chunks(session, qvec, namespace, limit)
    mem_hits = await repo.search_memories(session, qvec, namespace, limit)
    results = sorted(chunk_hits + mem_hits, key=lambda r: r["score"], reverse=True)[:limit]

    graph: list[dict] = []
    if include_graph and namespace:
        for ent in (await age.search_entities(session, query, namespace))[:3]:
            graph.extend(await age.get_related(session, ent["name"], namespace))

    return {"results": results, "graph": graph}
```

- [ ] **Step 2: Escrever os testes de integração**

`brain/tests/integration/test_retriever.py`:
```python
import pytest_asyncio

from brain.graph import age
from brain.search.retriever import search
from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base


def _vec(seed: float) -> list[float]:
    return [seed] * 2000


class FakeEmbedder:
    def __init__(self, mapping):
        self._m = mapping

    async def embed(self, texts):
        return [self._m[t] for t in texts]


@pytest_asyncio.fixture
async def session(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as s:
        await age.ensure_graph(s)
        from sqlalchemy import text
        await age._prepare(s)
        await s.execute(text(
            "SELECT * FROM cypher('brain', $cy$ MATCH (n) DETACH DELETE n $cy$) AS (v agtype)"
        ))
        await s.commit()
        yield s
    await engine.dispose()


async def test_busca_unifica_documentos_e_memorias_ordenado(session):
    doc = await repo.upsert_document(
        session, namespace="t", repo_path="a.md", title=None,
        raw_content="x", content_hash="h", commit_sha=None,
    )
    await repo.replace_chunks(
        session, doc.id, [{"ordinal": 0, "text": "doc perto", "token_count": 1}], [_vec(0.10)]
    )
    await repo.add_memory(session, namespace="t", content="mem longe", embedding=_vec(0.90))
    await session.commit()

    emb = FakeEmbedder({"consulta": _vec(0.11)})
    out = await search(session, emb, "consulta", namespace="t", limit=10)
    assert out["results"][0]["text"] == "doc perto"
    assert {r["source"] for r in out["results"]} == {"document", "memory"}


async def test_include_graph_traz_relacionados(session):
    await age.upsert_entity(session, "brain", "projeto", "t")
    await age.upsert_entity(session, "Renato", "pessoa", "t")
    await age.upsert_relation(session, "brain", "Renato", "owned_by", "t")
    await session.commit()

    emb = FakeEmbedder({"brain": _vec(0.5)})
    out = await search(session, emb, "brain", namespace="t", include_graph=True)
    assert any(g["name"] == "Renato" for g in out["graph"])
```

- [ ] **Step 3: Rodar os testes**

Run: `cd brain && uv run pytest tests/integration/test_retriever.py -v`
Expected: PASS (2 passed)

- [ ] **Step 4: Commit**

```bash
git add brain/src/brain/search brain/tests/integration/test_retriever.py
git commit -m "feat(brain): busca unificada vetorial + expansão por grafo"
```

---

## Task 16: Git sync (clone/pull + diff)

> Operações Git síncronas via `subprocess` (sem dependência extra). Testes usam repositórios Git locais em `tmp_path` — **sem rede**.

**Files:**
- Create: `brain/src/brain/ingestion/__init__.py`
- Create: `brain/src/brain/ingestion/git_sync.py`
- Create: `brain/tests/test_git_sync.py`

- [ ] **Step 1: Criar `ingestion/__init__.py` (vazio) e implementar `git_sync.py`**

`brain/src/brain/ingestion/git_sync.py`:
```python
import hashlib
import subprocess
from pathlib import Path


def _run(args: list[str], cwd: str | Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _auth_url(url: str, token: str | None) -> str:
    if token and url.startswith("https://"):
        return url.replace("https://", f"https://{token}@", 1)
    return url


def head_sha(dest: str | Path) -> str:
    return _run(["rev-parse", "HEAD"], cwd=dest).strip()


def clone_or_pull(repo_url: str, dest: str | Path, token: str | None = None) -> tuple[str | None, str]:
    """Retorna (sha_antes, sha_depois). sha_antes é None no primeiro clone."""
    dest = Path(dest)
    if (dest / ".git").exists():
        before = head_sha(dest)
        _run(["pull", "--rebase"], cwd=dest)
        return before, head_sha(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["clone", _auth_url(repo_url, token), str(dest)])
    return None, head_sha(dest)


def changed_files(dest: str | Path, old_sha: str | None, new_sha: str) -> list[tuple[str, str]]:
    """Lista (status, path) de arquivos .md alterados. status: A/M/D."""
    if old_sha is None:
        out = _run(["ls-files", "*.md"], cwd=dest)
        return [("A", p) for p in out.splitlines() if p]
    out = _run(["diff", "--name-status", old_sha, new_sha], cwd=dest)
    changes = []
    for line in out.splitlines():
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if path.endswith(".md"):
            changes.append((status[0], path))
    return changes
```

- [ ] **Step 2: Escrever os testes**

`brain/tests/test_git_sync.py`:
```python
import subprocess
from pathlib import Path

from brain.ingestion.git_sync import changed_files, clone_or_pull, content_hash, head_sha


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)


def test_content_hash_estavel():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


def test_clone_e_changed_files_no_primeiro_clone(tmp_path):
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "nota.md").write_text("# oi")
    _git(["add", "."], origin)
    _git(["commit", "-m", "1"], origin)

    dest = tmp_path / "clone"
    before, after = clone_or_pull(str(origin), dest)
    assert before is None
    assert len(after) == 40
    changes = changed_files(dest, None, after)
    assert ("A", "nota.md") in changes


def test_pull_detecta_diff(tmp_path):
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "a.md").write_text("# a")
    _git(["add", "."], origin)
    _git(["commit", "-m", "1"], origin)

    dest = tmp_path / "clone"
    _, sha1 = clone_or_pull(str(origin), dest)

    (origin / "b.md").write_text("# b")
    _git(["add", "."], origin)
    _git(["commit", "-m", "2"], origin)

    before, after = clone_or_pull(str(origin), dest)
    assert before == sha1
    changes = changed_files(dest, before, after)
    assert ("A", "b.md") in changes
```

- [ ] **Step 3: Rodar os testes**

Run: `cd brain && uv run pytest tests/test_git_sync.py -v`
Expected: PASS (3 passed)

- [ ] **Step 4: Commit**

```bash
git add brain/src/brain/ingestion/__init__.py brain/src/brain/ingestion/git_sync.py brain/tests/test_git_sync.py
git commit -m "feat(brain): git sync com clone/pull e diff de arquivos"
```

---

## Task 17: Git writer (escreve `.md` da conversa)

**Files:**
- Create: `brain/src/brain/ingestion/git_writer.py`
- Create: `brain/tests/test_git_writer.py`

- [ ] **Step 1: Implementar `git_writer.py`**

`brain/src/brain/ingestion/git_writer.py`:
```python
import re
import subprocess
from pathlib import Path


def _slugify(text: str) -> str:
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
```

- [ ] **Step 2: Escrever os testes**

`brain/tests/test_git_writer.py`:
```python
import subprocess
from pathlib import Path

from brain.ingestion.git_writer import render_markdown, write_conversation, _slugify


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)


def test_slugify():
    assert _slugify("Olá, Mundo!") == "ola-mundo"
    assert _slugify("") == "conversa"


def test_render_markdown():
    md = render_markdown([{"role": "user", "content": "oi"}])
    assert "**user:** oi" in md


def test_write_conversation_cria_arquivo_e_commit(tmp_path):
    repo = tmp_path / "vault"
    _init_repo(repo)
    rel = write_conversation(
        repo, "conversas", "trabalho",
        [{"role": "user", "content": "preciso lembrar disso"}],
        timestamp="20260604T120000",
        author_name="brain-bot",
        author_email="brain-bot@x",
        push=False,
    )
    assert rel.startswith("conversas/trabalho/20260604T120000-")
    assert (repo / rel).exists()
    # o último commit é do brain-bot
    author = _git(["log", "-1", "--format=%an"], repo).stdout.strip()
    assert author == "brain-bot"
```

- [ ] **Step 3: Rodar os testes**

Run: `cd brain && uv run pytest tests/test_git_writer.py -v`
Expected: PASS (3 passed)

- [ ] **Step 4: Commit**

```bash
git add brain/src/brain/ingestion/git_writer.py brain/tests/test_git_writer.py
git commit -m "feat(brain): git writer de conversas com commit brain-bot"
```

---

## Task 18: Pipeline de ingestão

**Files:**
- Create: `brain/src/brain/ingestion/pipeline.py`
- Create: `brain/tests/integration/test_pipeline.py`

- [ ] **Step 1: Implementar `pipeline.py`**

`brain/src/brain/ingestion/pipeline.py`:
```python
from brain.extraction.entities import extract_entities
from brain.extraction.facts import extract_facts
from brain.graph import age
from brain.indexing.chunker import chunk_markdown
from brain.ingestion.git_sync import content_hash
from brain.storage import repositories as repo


def _title(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return None


async def index_document(
    session, embedder, llm, settings, *, namespace, repo_path, content, commit_sha
) -> bool:
    """Indexa um documento. Retorna False se foi no-op (conteúdo inalterado)."""
    h = content_hash(content)
    existing = await repo.get_document(session, repo_path=repo_path)
    if existing and existing.content_hash == h:
        return False

    doc = await repo.upsert_document(
        session,
        namespace=namespace,
        repo_path=repo_path,
        title=_title(content),
        raw_content=content,
        content_hash=h,
        commit_sha=commit_sha,
    )
    chunks = chunk_markdown(content, settings.chunk_max_tokens, settings.chunk_overlap_tokens)
    embeddings = await embedder.embed([c["text"] for c in chunks]) if chunks else []
    await repo.replace_chunks(session, doc.id, chunks, embeddings)

    ents = await extract_entities(llm, content)
    await age.ensure_graph(session)
    for e in ents["entities"]:
        await age.upsert_entity(session, e["name"], e["type"], namespace, {"source_doc": repo_path})
    for r in ents["relations"]:
        await age.upsert_relation(session, r["source"], r["target"], r["type"], namespace)
    await session.commit()
    return True


async def extract_and_store_facts(session, embedder, llm, *, namespace, messages) -> list[dict]:
    facts = await extract_facts(llm, messages)
    for f in facts:
        (emb,) = await embedder.embed([f["content"]])
        await repo.add_memory(
            session,
            namespace=namespace,
            content=f["content"],
            embedding=emb,
            confidence=f["confidence"],
        )
    await session.commit()
    return facts
```

- [ ] **Step 2: Escrever os testes de integração**

`brain/tests/integration/test_pipeline.py`:
```python
import pytest_asyncio
from sqlalchemy import text

from brain.config import Settings
from brain.graph import age
from brain.ingestion import pipeline
from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base


def _settings() -> Settings:
    return Settings(
        database_url="x", openai_api_key="x", github_token="x",
        brain_auth_token="x", webhook_secret="x", repo_url="x",
    )


class FakeEmbedder:
    async def embed(self, texts):
        return [[0.1] * 2000 for _ in texts]


class FakeLLM:
    async def complete_json(self, system, user):
        if "entidades" in system:
            return {"entities": [{"name": "brain", "type": "projeto"}], "relations": []}
        return {"facts": [{"content": "gosta de python", "confidence": 0.8}]}


@pytest_asyncio.fixture
async def session(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as s:
        await age.ensure_graph(s)
        await age._prepare(s)
        await s.execute(text(
            "SELECT * FROM cypher('brain', $cy$ MATCH (n) DETACH DELETE n $cy$) AS (v agtype)"
        ))
        await s.commit()
        yield s
    await engine.dispose()


async def test_index_document_cria_doc_chunks_e_entidades(session):
    created = await pipeline.index_document(
        session, FakeEmbedder(), FakeLLM(), _settings(),
        namespace="t", repo_path="a.md", content="# Nota\nconteúdo sobre brain", commit_sha="abc",
    )
    assert created is True
    doc = await repo.get_document(session, repo_path="a.md")
    assert doc is not None and doc.title == "Nota"
    ent = await age.get_entity(session, "brain", "t")
    assert ent is not None


async def test_index_document_idempotente(session):
    args = dict(namespace="t", repo_path="a.md", content="# X\ncorpo", commit_sha=None)
    assert await pipeline.index_document(session, FakeEmbedder(), FakeLLM(), _settings(), **args) is True
    assert await pipeline.index_document(session, FakeEmbedder(), FakeLLM(), _settings(), **args) is False


async def test_extract_and_store_facts(session):
    facts = await pipeline.extract_and_store_facts(
        session, FakeEmbedder(), FakeLLM(), namespace="p",
        messages=[{"role": "user", "content": "eu uso python"}],
    )
    assert facts[0]["content"] == "gosta de python"
    mems = await repo.list_memories(session, "p")
    assert len(mems) == 1
```

- [ ] **Step 3: Rodar os testes**

Run: `cd brain && uv run pytest tests/integration/test_pipeline.py -v`
Expected: PASS (3 passed)

- [ ] **Step 4: Commit**

```bash
git add brain/src/brain/ingestion/pipeline.py brain/tests/integration/test_pipeline.py
git commit -m "feat(brain): pipeline de ingestão (index_document + extract_facts)"
```

---

## Task 19: Worker

**Files:**
- Create: `brain/src/brain/worker.py`
- Create: `brain/tests/integration/test_worker.py`

- [ ] **Step 1: Implementar `worker.py`**

`brain/src/brain/worker.py`:
```python
import asyncio
from pathlib import Path

import structlog

from brain.config import get_settings
from brain.indexing.embeddings import Embedder
from brain.extraction.llm import LLMClient
from brain.ingestion import pipeline
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory

log = structlog.get_logger()


async def handle_job(session, embedder, llm, settings, job) -> None:
    p = job.payload
    if job.type in ("index_document", "reindex"):
        content = (Path(settings.repo_cache_path) / p["repo_path"]).read_text(encoding="utf-8")
        await pipeline.index_document(
            session, embedder, llm, settings,
            namespace=p["namespace"], repo_path=p["repo_path"],
            content=content, commit_sha=p.get("commit_sha"),
        )
    elif job.type == "delete_document":
        await repo.delete_document_by_path(session, p["repo_path"])
        await session.commit()
    elif job.type == "extract_facts":
        await pipeline.extract_and_store_facts(
            session, embedder, llm, namespace=p["namespace"], messages=p["messages"]
        )
    else:
        raise ValueError(f"tipo de job desconhecido: {job.type}")


async def run_once(session_factory, queue, embedder, llm, settings, worker_id="worker") -> bool:
    job = await queue.claim_next(worker_id)
    if job is None:
        return False
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
```

- [ ] **Step 2: Escrever os testes de integração**

`brain/tests/integration/test_worker.py`:
```python
import pytest_asyncio

from brain.config import Settings
from brain.queue.base import JobType
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage import repositories as repo
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base
from brain.worker import run_once


class FakeEmbedder:
    async def embed(self, texts):
        return [[0.1] * 2000 for _ in texts]


class FakeLLM:
    async def complete_json(self, system, user):
        if "entidades" in system:
            return {"entities": [], "relations": []}
        return {"facts": [{"content": "usa python", "confidence": 0.9}]}


@pytest_asyncio.fixture
async def ctx(async_dsn, tmp_path):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    settings = Settings(
        database_url=async_dsn, openai_api_key="x", github_token="x",
        brain_auth_token="x", webhook_secret="x", repo_url="x",
        repo_cache_path=str(tmp_path),
    )
    yield sf, PostgresJobQueue(sf), settings, tmp_path
    await engine.dispose()


async def test_worker_processa_extract_facts(ctx):
    sf, queue, settings, _ = ctx
    await queue.enqueue(JobType.EXTRACT_FACTS.value, {
        "namespace": "p", "messages": [{"role": "user", "content": "eu uso python"}]
    })
    assert await run_once(sf, queue, FakeEmbedder(), FakeLLM(), settings) is True
    async with sf() as s:
        mems = await repo.list_memories(s, "p")
    assert len(mems) == 1


async def test_worker_processa_index_document(ctx):
    sf, queue, settings, tmp = ctx
    (tmp / "a.md").write_text("# Nota\ncorpo", encoding="utf-8")
    await queue.enqueue(JobType.INDEX_DOCUMENT.value, {"namespace": "t", "repo_path": "a.md"})
    assert await run_once(sf, queue, FakeEmbedder(), FakeLLM(), settings) is True
    async with sf() as s:
        doc = await repo.get_document(s, repo_path="a.md")
    assert doc is not None


async def test_worker_job_desconhecido_vai_para_failed(ctx):
    sf, queue, settings, _ = ctx
    jid = await queue.enqueue("tipo_invalido", {})
    # esgota tentativas
    for _ in range(settings.max_job_attempts):
        await run_once(sf, queue, FakeEmbedder(), FakeLLM(), settings)
    from sqlalchemy import text
    async with sf() as s:
        status = (await s.execute(
            text("SELECT status FROM ingestion_jobs WHERE id=:id"), {"id": jid}
        )).scalar_one()
    assert status == "failed"


async def test_run_once_sem_jobs_retorna_false(ctx):
    sf, queue, settings, _ = ctx
    assert await run_once(sf, queue, FakeEmbedder(), FakeLLM(), settings) is False
```

- [ ] **Step 3: Rodar os testes**

Run: `cd brain && uv run pytest tests/integration/test_worker.py -v`
Expected: PASS (4 passed)

- [ ] **Step 4: Commit**

```bash
git add brain/src/brain/worker.py brain/tests/integration/test_worker.py
git commit -m "feat(brain): worker de ingestão com run_once e loop"
```

---

> **Ajuste de config:** adicionar `git_push_enabled: bool = True` em `Settings` (Task 3), para desabilitar push nos testes. Acrescente o campo na classe `Settings` logo após `git_author_email` e um teste em `tests/test_config.py` cobrindo o default `True`.

---

## Task 20: Tools MCP (handlers + servidor)

> A lógica das tools fica em `mcp/handlers.py` (funções puras testáveis recebendo `deps`). `mcp/server.py` apenas registra wrappers no FastMCP.

**Files:**
- Create: `brain/src/brain/mcp/__init__.py`
- Create: `brain/src/brain/mcp/handlers.py`
- Create: `brain/src/brain/mcp/server.py`
- Create: `brain/tests/integration/test_mcp_handlers.py`

- [ ] **Step 1: Criar `mcp/__init__.py` (vazio) e implementar `handlers.py`**

`brain/src/brain/mcp/handlers.py`:
```python
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
```

- [ ] **Step 2: Implementar `server.py`**

`brain/src/brain/mcp/server.py`:
```python
from mcp.server.fastmcp import FastMCP

from brain.mcp import handlers
from brain.mcp.handlers import Deps


def create_mcp_server(deps: Deps) -> FastMCP:
    mcp = FastMCP("brain")

    @mcp.tool()
    async def remember(namespace: str, messages: list[dict], metadata: dict | None = None) -> dict:
        return await handlers.remember(deps, namespace, messages, metadata)

    @mcp.tool()
    async def search(query: str, namespace: str | None = None,
                     limit: int = 10, include_graph: bool = False) -> dict:
        return await handlers.search(deps, query, namespace, limit, include_graph)

    @mcp.tool()
    async def get_memory(id: str) -> dict | None:
        return await handlers.get_memory(deps, id)

    @mcp.tool()
    async def list_memories(namespace: str | None = None) -> list[dict]:
        return await handlers.list_memories(deps, namespace)

    @mcp.tool()
    async def update_memory(id: str, content: str | None = None) -> dict | None:
        return await handlers.update_memory(deps, id, content)

    @mcp.tool()
    async def move_memory(id: str, namespace: str) -> dict | None:
        return await handlers.move_memory(deps, id, namespace)

    @mcp.tool()
    async def delete_memory(id: str) -> dict:
        return await handlers.delete_memory(deps, id)

    @mcp.tool()
    async def merge_memories(ids: list[str], into: str | None = None) -> dict:
        return await handlers.merge_memories(deps, ids, into)

    @mcp.tool()
    async def get_document(id_or_path: str) -> dict | None:
        return await handlers.get_document(deps, id_or_path)

    @mcp.tool()
    async def list_documents(namespace: str | None = None) -> list[dict]:
        return await handlers.list_documents(deps, namespace)

    @mcp.tool()
    async def reindex(repo_path: str, namespace: str) -> dict:
        return await handlers.reindex(deps, repo_path, namespace)

    @mcp.tool()
    async def get_entity(name: str, namespace: str) -> dict | None:
        return await handlers.get_entity(deps, name, namespace)

    @mcp.tool()
    async def search_entities(query: str, namespace: str) -> list[dict]:
        return await handlers.search_entities(deps, query, namespace)

    @mcp.tool()
    async def get_related(entity: str, namespace: str, depth: int = 1) -> list[dict]:
        return await handlers.get_related(deps, entity, namespace, depth)

    @mcp.tool()
    async def update_entity(name: str, namespace: str, props: dict) -> dict:
        return await handlers.update_entity(deps, name, namespace, props)

    @mcp.tool()
    async def merge_entities(sources: list[str], into: str, namespace: str) -> dict:
        return await handlers.merge_entities(deps, sources, into, namespace)

    @mcp.tool()
    async def delete_entity(name: str, namespace: str) -> dict:
        return await handlers.delete_entity(deps, name, namespace)

    @mcp.tool()
    async def create_namespace(name: str, description: str | None = None) -> dict:
        return await handlers.create_namespace(deps, name, description)

    @mcp.tool()
    async def list_namespaces() -> list[dict]:
        return await handlers.list_namespaces(deps)

    return mcp
```

- [ ] **Step 3: Escrever os testes de integração dos handlers (subconjunto representativo)**

`brain/tests/integration/test_mcp_handlers.py`:
```python
import subprocess

import pytest_asyncio

from brain.config import Settings
from brain.mcp import handlers
from brain.mcp.handlers import Deps
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage.db import make_engine, make_session_factory
from brain.storage.models import Base
from brain.storage import repositories as repo


class FakeEmbedder:
    async def embed(self, texts):
        return [[0.2] * 2000 for _ in texts]


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    for args in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


@pytest_asyncio.fixture
async def deps(async_dsn, tmp_path):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    vault = tmp_path / "vault"
    _init_repo(vault)
    settings = Settings(
        database_url=async_dsn, openai_api_key="x", github_token="x",
        brain_auth_token="x", webhook_secret="x", repo_url="x",
        repo_cache_path=str(vault), git_push_enabled=False,
    )
    yield Deps(sf, FakeEmbedder(), None, PostgresJobQueue(sf), settings)
    await engine.dispose()


async def test_remember_grava_nota_e_enfileira(deps):
    out = await handlers.remember(deps, "trabalho", [{"role": "user", "content": "lembrar disso"}])
    assert out["note_path"].startswith("conversas/trabalho/")
    assert len(out["job_ids"]) == 2


async def test_namespaces_crud(deps):
    await handlers.create_namespace(deps, "t", "trabalho")
    names = [n["name"] for n in await handlers.list_namespaces(deps)]
    assert "t" in names


async def test_memoria_crud_via_handlers(deps):
    async with deps.session_factory() as s:
        m = await repo.add_memory(s, namespace="p", content="gosta de chá", embedding=[0.2] * 2000)
        await s.commit()
        mid = str(m.id)
    got = await handlers.get_memory(deps, mid)
    assert got["content"] == "gosta de chá"
    await handlers.move_memory(deps, mid, "trabalho")
    assert (await handlers.get_memory(deps, mid))["namespace"] == "trabalho"
    assert (await handlers.delete_memory(deps, mid))["deleted"] is True


async def test_reindex_enfileira(deps):
    out = await handlers.reindex(deps, "a.md", "t")
    assert "job_id" in out
```

- [ ] **Step 4: Rodar os testes**

Run: `cd brain && uv run pytest tests/integration/test_mcp_handlers.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add brain/src/brain/mcp brain/tests/integration/test_mcp_handlers.py
git commit -m "feat(brain): tools MCP (handlers + servidor FastMCP)"
```

---

> **Ajuste no servidor MCP:** em `mcp/server.py` (Task 20), criar o servidor com `FastMCP("brain", stateless_http=True)` (sem sessão persistente — adequado a uso pessoal e simplifica a montagem na API). Atualize a linha `mcp = FastMCP("brain")` para `mcp = FastMCP("brain", stateless_http=True)`.

---

## Task 21: API FastAPI (webhook, health, status, MCP montado)

**Files:**
- Create: `brain/src/brain/main.py`
- Create: `brain/tests/test_signature.py`
- Create: `brain/tests/integration/test_main.py`

- [ ] **Step 1: Implementar `main.py`**

`brain/src/brain/main.py`:
```python
import hashlib
import hmac
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from starlette.types import ASGIApp, Receive, Scope, Send

from brain.config import get_settings
from brain.extraction.llm import LLMClient
from brain.indexing.embeddings import Embedder
from brain.ingestion import git_sync
from brain.mcp.handlers import Deps
from brain.mcp.server import create_mcp_server
from brain.queue.base import JobType
from brain.queue.postgres_queue import PostgresJobQueue
from brain.storage.db import make_engine, make_session_factory

log = structlog.get_logger()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class _BearerAuth:
    """Middleware ASGI que protege o app MCP montado."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if auth != f"Bearer {self.token}":
                await send({
                    "type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def build_deps(settings):
    engine = make_engine(settings.database_url)
    sf = make_session_factory(engine)
    deps = Deps(
        sf,
        Embedder.from_settings(settings),
        LLMClient.from_settings(settings),
        PostgresJobQueue(sf),
        settings,
    )
    return deps, sf


def create_app(deps: Deps, sf) -> FastAPI:
    settings = deps.settings
    mcp = create_mcp_server(deps)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="brain", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> dict:
        async with sf() as s:
            rows = (await s.execute(
                text("SELECT status, count(*) FROM ingestion_jobs GROUP BY status")
            )).all()
        return {"jobs": {r[0]: r[1] for r in rows}}

    @app.post("/webhook/github")
    async def webhook(request: Request):
        body = await request.body()
        if not verify_signature(
            settings.webhook_secret, body, request.headers.get("X-Hub-Signature-256")
        ):
            return Response(status_code=401)
        before, after = git_sync.clone_or_pull(
            settings.repo_url, settings.repo_cache_path, settings.github_token
        )
        enqueued = 0
        for code, path in git_sync.changed_files(settings.repo_cache_path, before, after):
            namespace = path.split("/")[0]
            if code == "D":
                await deps.queue.enqueue(JobType.DELETE_DOCUMENT.value, {"repo_path": path})
            else:
                await deps.queue.enqueue(
                    JobType.INDEX_DOCUMENT.value,
                    {"namespace": namespace, "repo_path": path, "commit_sha": after},
                )
            enqueued += 1
        return {"enqueued": enqueued}

    app.mount("/mcp", _BearerAuth(mcp_app, settings.brain_auth_token))
    return app


# App de produção (uvicorn brain.main:app). Só constrói se houver env configurado.
app = create_app(*build_deps(get_settings())) if os.getenv("DATABASE_URL") else None
```

- [ ] **Step 2: Escrever o teste unitário da assinatura**

`brain/tests/test_signature.py`:
```python
import hashlib
import hmac

from brain.main import verify_signature


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_assinatura_valida():
    body = b'{"ok":true}'
    assert verify_signature("seg", body, _sig("seg", body)) is True


def test_assinatura_invalida():
    assert verify_signature("seg", b"x", "sha256=deadbeef") is False


def test_header_ausente():
    assert verify_signature("seg", b"x", None) is False
```

- [ ] **Step 3: Rodar o teste unitário**

Run: `cd brain && uv run pytest tests/test_signature.py -v`
Expected: PASS (3 passed)

- [ ] **Step 4: Escrever o teste de integração da API**

`brain/tests/integration/test_main.py`:
```python
import hashlib
import hmac

import pytest_asyncio
from fastapi.testclient import TestClient

from brain import main
from brain.config import Settings
from brain.main import build_deps, create_app
from brain.storage.db import make_engine
from brain.storage.models import Base


@pytest_asyncio.fixture
async def prepared_db(async_dsn):
    engine = make_engine(async_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


def _settings(async_dsn, tmp_path) -> Settings:
    return Settings(
        database_url=async_dsn, openai_api_key="x", github_token="x",
        brain_auth_token="tok", webhook_secret="seg", repo_url="https://x/y.git",
        repo_cache_path=str(tmp_path), git_push_enabled=False,
    )


def test_health(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_status_retorna_contagem(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    with TestClient(app) as client:
        body = client.get("/status").json()
    assert "jobs" in body


def test_webhook_rejeita_assinatura_invalida(async_dsn, tmp_path, prepared_db):
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    with TestClient(app) as client:
        r = client.post("/webhook/github", content=b"{}", headers={"X-Hub-Signature-256": "sha256=x"})
    assert r.status_code == 401


def test_webhook_enfileira_jobs(async_dsn, tmp_path, prepared_db, monkeypatch):
    monkeypatch.setattr(main.git_sync, "clone_or_pull", lambda *a, **k: ("old", "new"))
    monkeypatch.setattr(
        main.git_sync, "changed_files",
        lambda *a, **k: [("A", "trabalho/nota.md"), ("D", "trabalho/old.md")],
    )
    app = create_app(*build_deps(_settings(async_dsn, tmp_path)))
    body = b'{"ref":"refs/heads/main"}'
    sig = "sha256=" + hmac.new(b"seg", body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        r = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.json() == {"enqueued": 2}
```

- [ ] **Step 5: Rodar o teste de integração**

Run: `cd brain && uv run pytest tests/integration/test_main.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add brain/src/brain/main.py brain/tests/test_signature.py brain/tests/integration/test_main.py
git commit -m "feat(brain): API FastAPI com webhook, health, status e MCP montado"
```

---

## Task 22: Empacotamento Docker e deploy

**Files:**
- Create: `brain/Dockerfile`
- Create: `brain/docker-compose.yml`
- Create: `brain/.env.example`
- Create: `brain/README.md`

- [ ] **Step 1: Criar o `Dockerfile` da aplicação**

`brain/Dockerfile`:
```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "brain.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Criar o `docker-compose.yml`**

`brain/docker-compose.yml`:
```yaml
services:
  postgres:
    build: ./docker/postgres
    environment:
      POSTGRES_USER: brain
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: brain
    command: ["postgres", "-c", "shared_preload_libraries=age"]
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U brain"]
      interval: 5s
      timeout: 5s
      retries: 10

  api:
    build: .
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
    command: ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn brain.main:app --host 0.0.0.0 --port 8000"]
    ports:
      - "8000:8000"

  worker:
    build: .
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
    command: ["uv", "run", "python", "-m", "brain.worker"]

volumes:
  pgdata:
```

- [ ] **Step 3: Criar `.env.example`**

`brain/.env.example`:
```dotenv
POSTGRES_PASSWORD=troque-me
DATABASE_URL=postgresql+asyncpg://brain:troque-me@postgres:5432/brain
OPENAI_API_KEY=sk-...
GITHUB_TOKEN=ghp_...
BRAIN_AUTH_TOKEN=gere-um-token-forte
WEBHOOK_SECRET=gere-um-segredo
REPO_URL=https://github.com/usuario/brain-vault.git
REPO_CACHE_PATH=repo_cache
GIT_PUSH_ENABLED=true
```

- [ ] **Step 4: Criar `README.md`**

`brain/README.md`:
````markdown
# brain — Provedor de Memória (MCP)

Provedor de memória pessoal exposto como servidor MCP. Indexa um repositório
GitHub de markdown, extrai fatos de conversas, mantém um grafo de entidades e
serve busca semântica unificada.

## Subir em produção (VPS)

```bash
cp .env.example .env   # preencha os segredos
docker compose build   # compila Postgres (pgvector+AGE) e a app
docker compose up -d
curl http://localhost:8000/health   # -> {"status":"ok"}
```

Coloque um reverse proxy (Caddy/Traefik) na frente para TLS no endpoint `/mcp`.

## Configurar o webhook do GitHub

No repositório do vault: Settings → Webhooks → Add webhook.
- Payload URL: `https://SEU_DOMINIO/webhook/github`
- Content type: `application/json`
- Secret: o mesmo valor de `WEBHOOK_SECRET`
- Eventos: apenas `push`

## Conectar um cliente MCP

Endpoint: `https://SEU_DOMINIO/mcp` (transporte streamable HTTP).
Header: `Authorization: Bearer <BRAIN_AUTH_TOKEN>`.

## Desenvolvimento

```bash
docker build -t brain-postgres:local docker/postgres   # imagem usada pelos testes
uv sync
uv run pytest
```
````

- [ ] **Step 5: Validar o build do compose (manual)**

Run: `cd brain && docker compose build`
Expected: build de `postgres`, `api` e `worker` conclui sem erro.

- [ ] **Step 6: Subir e validar health (manual)**

Run: `cd brain && cp .env.example .env && docker compose up -d && sleep 20 && curl -s http://localhost:8000/health`
Expected: `{"status":"ok"}`. Depois: `docker compose down`.

- [ ] **Step 7: Commit**

```bash
git add brain/Dockerfile brain/docker-compose.yml brain/.env.example brain/README.md
git commit -m "feat(brain): empacotamento Docker, compose e documentação de deploy"
```

---

## Task 23: Verificação final da suíte

- [ ] **Step 1: Garantir a imagem de testes construída**

Run: `docker build -t brain-postgres:local brain/docker/postgres`
Expected: imagem disponível (necessária pelos testes de integração).

- [ ] **Step 2: Rodar a suíte completa**

Run: `cd brain && uv run pytest -v`
Expected: todos os testes passam (unit + integração). Os de integração sobem o container via testcontainers.

- [ ] **Step 3: Commit final (se houver ajustes)**

```bash
git add -A
git commit -m "test(brain): suíte completa verde"
```

---

## Cobertura do spec (auto-revisão)

| Requisito do spec | Task(s) |
|---|---|
| Servidor MCP + auth bearer | 4, 20, 21 |
| Postgres pgvector + Apache AGE | 2, 5, 6 |
| Extração de fatos (OpenAI) | 10, 11, 18 |
| Embeddings `text-embedding-3-large` (2000) | 9, 5 |
| Busca semântica unificada (chunks + memories) | 14, 15 |
| Ingestão via repo GitHub + webhook + diff | 16, 21 |
| Histórico bruto em `.md` (brain grava/commit/push) | 17, 20 |
| Grafo de entidades/relações | 12, 13, 18 |
| Fila durável Postgres (SKIP LOCKED) + dead-letter | 7, 19 |
| Tools de gerenciamento (move/merge/delete) | 14, 20 |
| Namespaces | 14, 20 |
| Tratamento de erros sem falha silenciosa | 7, 19, 21 |
| `/health` e `/status` | 21 |
| Deploy Docker (api + worker + postgres) | 2, 22 |
| Idempotência por `content_hash` / anti-loop `brain-bot` | 16, 17, 18 |

**Itens fora do escopo (spec §12, confirmado):** adaptadores Redis/RabbitMQ, worker em host separado, cache de embeddings, agente curador Hermes.
