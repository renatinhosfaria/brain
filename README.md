# brain

`brain` é um serviço **FastAPI + MCP** que mantém uma camada pesquisável sobre um
**vault Markdown curado** ([brain-vault](https://github.com/renatinhosfaria/brain-vault)).
Clientes de agente pesquisam e leem notas curadas via MCP; submissões brutas
entram em `_agents/` e são promovidas a notas curadas pelo curador **Hermes**.

A recuperação é **híbrida**: `pgvector` para busca semântica sobre os chunks e
**Apache AGE** para percorrer um grafo de entidades e relações (`deep_search`).

## Stack

- Python 3.12 + [uv](https://docs.astral.sh/uv/)
- FastAPI + servidor MCP (FastMCP, transporte HTTP streamable)
- PostgreSQL com `pgvector` + Apache AGE
- SQLAlchemy (async) + Alembic
- OpenAI (embeddings e extração de entidades)
- Docker Compose (api, worker, postgres; perfis opcionais `proxy` e `backup`)

## Início rápido

```bash
test -f .env || cp .env.example .env   # preencha os segredos antes de subir
docker compose build
docker compose up -d
curl http://localhost:8000/health        # {"status":"ok","database":"ok"}
```

Detalhes de variáveis de ambiente, deploy, backup/restore e troubleshooting em
[docs/operations.md](docs/operations.md).

## Estrutura

```
src/brain/          código da aplicação (api, mcp, ingestão, busca, grafo, fila)
migrations/         migrações Alembic
docker/             imagem Postgres (pgvector + AGE), backup, proxy
docs/               documentação técnica (abaixo)
tests/              testes unitários e de integração (testcontainers)
```

## Documentação

- [docs/architecture.md](docs/architecture.md) — visão de containers, componentes e fluxos
- [docs/data-model.md](docs/data-model.md) — tabelas, grafo AGE e regras de reconstrução
- [docs/mcp-api.md](docs/mcp-api.md) — ferramentas MCP, permissões e contratos
- [docs/operations.md](docs/operations.md) — deploy, variáveis, backup e troubleshooting
- [docs/security.md](docs/security.md) — modelo de confiança, autenticação e segredos
- [docs/development.md](docs/development.md) — convenções de desenvolvimento
- [docs/decisions/](docs/decisions/) — registros de decisões arquiteturais

## Desenvolvimento

Pré-requisito: [uv](https://docs.astral.sh/uv/) instalado.

```bash
uv sync                       # instala dependências (inclui o grupo dev)

uv run ruff check src tests migrations      # lint
uv run ruff format src tests migrations     # formatação
uv run mypy                                  # checagem de tipos

uv run pytest tests --ignore=tests/integration   # testes unitários (rápidos)
```

Os **testes de integração** sobem um PostgreSQL com pgvector + AGE via
testcontainers; é preciso ter a imagem construída e Docker disponível:

```bash
docker build -t brain-postgres:local docker/postgres
uv run pytest tests/integration
```

Lint, tipos e ambas as suítes rodam automaticamente no CI
([.github/workflows/ci.yml](.github/workflows/ci.yml)) em cada push e pull request.

## Segurança

O vault é privado e `_agents/` guarda conteúdo bruto sensível. Não comite
segredos: `.env` está fora do versionamento e o startup rejeita placeholders.
Veja [docs/security.md](docs/security.md).
