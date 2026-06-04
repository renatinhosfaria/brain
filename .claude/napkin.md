# Napkin

## Corrections
| Date | Source | What Went Wrong | What To Do Instead |
|------|--------|----------------|-------------------|
| 2026-06-04 | user | Escrevi em inglês | O usuário fala português — responder SEMPRE em português (texto, perguntas, mensagens de commit já são pt por convenção do plano) |

## User Preferences
- Idioma: **português** em toda comunicação.

## Patterns That Work
- `uv` instalado em ~/.local/bin; prefixar `export PATH="$HOME/.local/bin:$PATH"` nos comandos Bash (env não persiste entre chamadas).
- Imagem `brain-postgres:local` builda em ~1min (AGE compilado). Testcontainers sobe ela rápido.

## Patterns That Don't Work
- (acumular aqui)

## Lacunas do plano (corrigidas durante execução)
- Plano usa `sync_dsn` (DSN psycopg2 do testcontainers) nos testes de infra, mas NÃO lista `psycopg2`. `testcontainers[postgres]` 4.14 não traz psycopg2. **Correção:** `uv add --dev psycopg2-binary` (Task 2).
- `uv` escolhe Python mais novo (3.14) por padrão; fixei 3.12 via `.python-version` (Task 1) para casar com `python:3.12-slim` de prod.
- **BUG do plano (Task 5, db.py):** o hook `register_vector` do `pgvector.asyncpg` CONFLITA com o tipo `pgvector.sqlalchemy.Vector`. O tipo SQLAlchemy já serializa a lista para o literal `'[...]'`; o codec asyncpg recebe essa string e falha ("could not convert string to float"). **Correção:** remover o event listener `register_vector` de `make_engine`. Via ORM o tipo Vector basta (inclusive `cosine_distance`). Não reintroduzir.
- **BUG do plano (Task 6, search_path):** o grafo AGE chama-se `brain` e cria um schema `brain`; o usuário do banco também é `brain`, então o `"$user"` do search_path padrão (`"$user", public`) passa a apontar para o schema do grafo e `CREATE TABLE` sem schema cai lá em vez de `public`. `test_models` passou por acaso (DDL+DML no mesmo path). **Correção:** `connect_args={"server_settings": {"search_path": "public"}}` em `make_engine` (db.py) E no engine da migration (migrations/env.py). `graph/age.py` seta o próprio search_path nas operações de grafo. Leituras seguem OK porque `public` continua no path.

## Domain Notes
- Projeto `brain`: provedor de memória pessoal como servidor MCP. Plano em `docs/superpowers/plans/2026-06-04-brain-memory-provider.md`, spec em `docs/superpowers/specs/2026-06-03-brain-memory-provider-design.md`.
- Stack: Python 3.12 + `uv`, SQLAlchemy async, Postgres custom (pgvector + Apache AGE), MCP/FastAPI, OpenAI.
- Ambiente (2026-06-04): `/root/brain` NÃO era repo git; `uv` NÃO instalado; Docker OK (29.1.4); Python do sistema 3.10.
- Execução do plano: TDD obrigatório, commits em pt-BR com Conventional Commits, todo o projeto vive em `brain/`.
