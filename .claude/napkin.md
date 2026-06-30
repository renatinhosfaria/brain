# Napkin

## Corrections
| Date | Source | What Went Wrong | What To Do Instead |
|------|--------|----------------|-------------------|
| 2026-06-04 | user | Escrevi em inglês | O usuário fala português — responder SEMPRE em português (texto, perguntas, mensagens de commit já são pt por convenção do plano) |

## User Preferences
- Idioma: **português** em toda comunicação.

## Estado do projeto (2026-06-04)
- Plano `2026-06-04-brain-memory-provider.md` 100% executado: 23 tarefas, 63 testes verdes (unit + integração). Mergeado na `main` (commit de merge ef36534); branch feat/brain-memory-provider apagada.
- **PENDENTE:** push para o remoto `origin` (https://github.com/renatinhosfaria/brain). Repo remoto estava vazio; ainda NÃO houve push (sem GITHUB_TOKEN no ambiente). Nada da `main` local foi enviado.
- Stack Docker validado: `docker compose build` + `up` sobe postgres+api+worker; migration aplica no boot; `/health` → ok.

## Patterns That Work
- `uv` instalado em ~/.local/bin; prefixar `export PATH="$HOME/.local/bin:$PATH"` nos comandos Bash (env não persiste entre chamadas). Rodar testes: `cd /root/brain && uv run pytest` (o projeto foi achatado: agora `src/` e `pyproject.toml` vivem direto em `/root/brain`, NÃO mais em `/root/brain/brain`).
- Imagem `brain-postgres:local` builda em ~1min (AGE compilado). Testcontainers sobe ela rápido. Testes de integração precisam dela construída.

## Patterns That Don't Work
- (acumular aqui)

## Features P2 (2026-06-22)
- **Rate limiting MCP**: `src/brain/ratelimit.py` (token bucket in-memory por principal). `MCP_RATE_LIMIT_PER_MINUTE` (0=off, padrão). `handlers.configure_rate_limiter(settings)` chamado em `create_mcp_server`; `_enforce_rate_limit` nos 4 `_require_*`. Global `_rate_limiter` fica None fora do server (testes diretos de handler não são afetados). Erro: `RateLimitExceeded`.
- **Reranking LLM opcional**: `src/brain/search/rerank.py` (`rerank(llm, query, results, top_n)`). `RERANK_ENABLED` (padrão off) + `RERANK_CANDIDATES` (pool, padrão 20). Integrado em `retriever._ranked_chunks` (usado por search e deep_search). Degrada para ordem vetorial em falha/resposta inválida. `search` agora aceita `llm=`.
- **Temporalidade no grafo**: entidades/relações ganham `valid_at`/`invalid_at`. `upsert_entity/relation` usam `coalesce(n.valid_at, now)` + `invalid_at=NULL` (reativa). `age.invalidate_entities_by_source_doc` (soft) é usada no reindex (pipeline) em vez de delete — preserva histórico; `delete_document` segue deletando. `get_relationship_paths(as_of=...)` filtra via `_validity_predicate` (nós+arestas). `deep_search`/handler/server expõem `as_of`. `get_entity` retorna valid_at/invalid_at.

## Avaliação 2026-06-22 (estado atual — design MUDOU de "memory provider" p/ "vault curado")
- O design evoluiu: hoje é serviço FastAPI+MCP sobre um **vault Markdown curado** (repo `brain-vault`), com inbox `_agents/` + curador **Hermes**. As notas antigas deste napkin sobre `remember`/`metadata` são do design ANTIGO (memory provider) — parcialmente obsoletas.
- **memories/extract_facts = subsistema REMOVIDO (2026-06-22)**: era órfão (nada enfileirava `extract_facts`, tools MCP nunca registradas em `server.py`). Removidos: modelo Memory, repos, handlers, `remember`, `pipeline.extract_and_store_facts`, `extraction/facts.py`, job EXTRACT_FACTS, testes; migration `0004_drop_memories` dropa a tabela. PRESERVADA a primitiva `source_memory` no grafo AGE (proveniência genérica, agora não preenchida). Commits `3cd1761` (refactor) + `4bd8924` (docs). Validado: 110 unit + 280 integração verdes.
- **20 commits locais NÃO pushados** (origin/main em 1fa75f3 / 2026-06-19; HEAD local 9d08dc1 / 2026-06-20). Sem GITHUB_TOKEN no ambiente → trabalho recente (semantic entities) só existe local. RISCO de perda.
- **CI/CD adicionado (2026-06-22)**: `.github/workflows/ci.yml` com jobs `lint` (ruff check + ruff format --check + mypy), `unit` (uv + pytest) e `integration` (builda `docker/postgres` como `brain-postgres:local` + testcontainers, `TESTCONTAINERS_RYUK_DISABLED=true`). Roda em push/main e PR. Token `gh` tem escopos `repo`+`workflow` (napkin antigo dizia "sem GITHUB_TOKEN" — DESATUALIZADO; usar `gh auth setup-git` p/ push).
- **ruff + mypy formalizados (2026-06-22)**: config em `pyproject.toml`. ruff `line-length=100`, select `E,F,I,W,UP,B,C4,SIM,BLE`; rodou `ruff format` no codebase (34 arquivos). mypy `files=["src"]`, `ignore_missing_imports`, `check_untyped_defs`; **verde (0 issues)**. Correção-chave: `Deps` dataclass agora é tipado (Settings/JobQueue/Embedder/LLMClient/async_sessionmaker) — eliminou ~77 erros. Comandos: `uv run ruff check src tests migrations`, `uv run ruff format --check ...`, `uv run mypy`.
- **README na raiz criado (2026-06-22)**: `README.md` com stack, quick start, estrutura, links p/ docs/ e fluxo de dev (ruff/mypy/pytest).
- **brain-vault tem README agora (2026-06-22)**: commit `a73db55` documenta taxonomia (projetos/processos/decisoes/preferencias/pessoas/organizacoes/systems/logs/_agents) e fluxo de curadoria.
- **brain-vault sem README/MOC/índice** — taxonomia (projetos/processos/decisoes/preferencias/pessoas/organizacoes/systems/logs/_agents) não documentada; navegação humana/Obsidian sem mapa.
- **Frontmatter de nota curada redundante**: `source_agent_note_ids` aparece dentro de `metadata:` E na raiz (handlers `_curated_frontmatter`).
- **Sem reranking** (busca = cosine + grafo, ordena por score) e **sem rate limiting** no MCP/HTTP. Auth = bearer estático (SHA-256+Fernet), não OAuth 2.1 (spec MCP nova). `.env` NÃO é tracked (ok).
- `/root/brain/brain/` é LIXO local (0 arquivos tracked; sobra do achatamento). Pode apagar do ambiente; não afeta o repo.
- Testes: `uv run pytest tests/ --ignore=tests/integration` → 112 passam (~10s). Integração exige imagem postgres custom (testcontainers).

## Auditoria 2026-06-29
- **origin/main SINCRONIZADO** (HEAD local == origin/main == b67063a). O "20 commits não pushados" do napkin antigo está OBSOLETO — push aconteceu. Features P2 (reranking/rate limit/temporalidade) commitadas em dc1d602/76af6bf.
- **WIP não commitado (2026-06-29):** identidade de committer (`git_author_name/email`) propagada ao `pull --rebase` em `git_sync.clone_or_pull` e `git_writer._push_with_retry`/`push_repo`/`main.py`. Corrige rebase falhando quando `repo_cache` não tem `user.name/email` global. Fix correto. **PORÉM:** `tests/test_git_sync.py` e `tests/test_git_writer.py` FALHAM em `ruff format --check` → job de lint do CI quebraria. Rodar `ruff format` antes de commitar.
- ~~**VULN (SQLi via dollar-quote no AGE)**~~ **CORRIGIDA (2026-06-30):** adicionado `_safe_cypher(body, as_clause)` em `age.py` que usa tag aleatória `$cy_<16hex>$` verificada como ausente no body antes de embrulhar. Todos os 15 sites de query em `age.py` migrados para `_safe_cypher`. 7 novos testes unitários em `tests/test_age_unit.py`. `ruff`/`mypy` limpos, 132 unit tests verdes. **Não há mais `$cy$` hardcoded no código produtivo.**
- Estado verificado (2026-06-30): `ruff check` ✅, `ruff format --check` ✅, `mypy` ✅, `pytest` unit ✅ 132 (~10s). Integração NÃO rodada (precisa imagem postgres custom).
- `/root/brain/brain/` (lixo do achatamento) AINDA existe local com migrations/src/tests não-tracked. Inofensivo ao repo.

## Lacunas/bugs ainda ABERTOS (não corrigidos)
- `remember` (handlers.py:51) ACEITA o parâmetro `metadata` mas NUNCA o usa/grava — silenciosamente descartado. A tabela `documents` (models.py) não tem coluna de autor; só `memories.meta` (jsonb) existe e também não é preenchido por extract_and_store_facts. Resultado: nota de conversa registra QUANDO (nome do arquivo + commit + created_at) mas não QUEM (commit é sempre brain-bot; só o namespace marca contexto). O texto do .md (git_writer.render_markdown) também não inclui timestamp/autor no corpo. Se for pedir "quem criou/quando", corrigir: cabeçalho no .md + persistir metadata.

## Lacunas do plano (corrigidas durante execução)
- Plano usa `sync_dsn` (DSN psycopg2 do testcontainers) nos testes de infra, mas NÃO lista `psycopg2`. `testcontainers[postgres]` 4.14 não traz psycopg2. **Correção:** `uv add --dev psycopg2-binary` (Task 2).
- `uv` escolhe Python mais novo (3.14) por padrão; fixei 3.12 via `.python-version` (Task 1) para casar com `python:3.12-slim` de prod.
- **BUG do plano (Task 5, db.py):** o hook `register_vector` do `pgvector.asyncpg` CONFLITA com o tipo `pgvector.sqlalchemy.Vector`. O tipo SQLAlchemy já serializa a lista para o literal `'[...]'`; o codec asyncpg recebe essa string e falha ("could not convert string to float"). **Correção:** remover o event listener `register_vector` de `make_engine`. Via ORM o tipo Vector basta (inclusive `cosine_distance`). Não reintroduzir.
- **BUG do plano (Task 17, _slugify):** `\w` do Python casa Unicode, então acentos sobreviviam (`olá-mundo`), mas o teste espera ASCII (`ola-mundo`). **Correção:** `unicodedata.normalize("NFKD", t).encode("ascii","ignore").decode()` antes do regex.
- **BUG do plano (Tasks 14/15, vetores de teste):** helper `_vec(seed)=[seed]*2000` cria vetores CONSTANTES, todos paralelos → distância de cosseno 0 entre quaisquer dois. Testes que checam ordenação por proximidade falham (empate, ordem arbitrária). **Correção:** `_vec(seed)=[1.0, seed]+[0.0]*1998` (direção varia com seed). Aplicar o mesmo em test_retriever.py (Task 15). Onde o vetor só é armazenado/recuperado (pipeline/worker/mcp_handlers), constante tudo bem.
- **BUG do plano (Task 13, age.merge_entities):** `text()` do SQLAlchemy trata `:REL` como bind param quando o `:` vem logo após `[` (aresta anônima `[:REL {...}]`). Labels com variável antes (`[r:REL]`, `(n:Entity)`) são ignorados pois o `:` vem após letra. **Correção:** dar variável descartável às arestas anônimas do MERGE (`[nr:REL {...}]`). Regra geral p/ Cypher via text(): nunca deixar `:label` precedido de não-letra.
- **BUG do plano (Task 8, chunker):** `token_count` re-encodando o texto decodificado da janela podia estourar `max_tokens` (BPE soma tokens nas bordas). **Correção:** `_split_by_tokens` retorna `(texto, len(window))` e `token_count` usa o tamanho da janela fatiada.
- **BUG do plano (Task 6, search_path):** o grafo AGE chama-se `brain` e cria um schema `brain`; o usuário do banco também é `brain`, então o `"$user"` do search_path padrão (`"$user", public`) passa a apontar para o schema do grafo e `CREATE TABLE` sem schema cai lá em vez de `public`. `test_models` passou por acaso (DDL+DML no mesmo path). **Correção:** `connect_args={"server_settings": {"search_path": "public"}}` em `make_engine` (db.py) E no engine da migration (migrations/env.py). `graph/age.py` seta o próprio search_path nas operações de grafo. Leituras seguem OK porque `public` continua no path.

## Domain Notes
- Projeto `brain`: provedor de memória pessoal como servidor MCP. Plano em `docs/superpowers/plans/2026-06-04-brain-memory-provider.md`, spec em `docs/superpowers/specs/2026-06-03-brain-memory-provider-design.md`.
- Stack: Python 3.12 + `uv`, SQLAlchemy async, Postgres custom (pgvector + Apache AGE), MCP/FastAPI, OpenAI.
- Ambiente (2026-06-04): `/root/brain` NÃO era repo git; `uv` NÃO instalado; Docker OK (29.1.4); Python do sistema 3.10.
- Execução do plano: TDD obrigatório, commits em pt-BR com Conventional Commits, todo o projeto vive em `brain/`.
