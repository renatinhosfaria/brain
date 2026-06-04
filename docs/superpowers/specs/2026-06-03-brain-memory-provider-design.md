# brain — Provedor de Memória Externo (Design / Spec)

- **Data:** 2026-06-03
- **Autor:** renatinhosfaria@gmail.com
- **Status:** Aprovado para implementação
- **Escopo deste spec:** Apenas o **provedor de memória** (`brain`). O agente curador (framework Hermes) é um projeto separado e futuro que consome este provedor via MCP.

---

## 1. Objetivo

Construir um **provedor de memória externo, pessoal e auto-hospedado** para agentes de IA e LLMs, exposto como **servidor MCP**, com capacidades equivalentes (combinadas) a Mem0, Supermemory e Honcho:

- **Extração automática de fatos** de conversas (estilo Mem0).
- **Busca semântica (recall)** sobre memórias e documentos.
- **Ingestão de documentos** a partir de um repositório GitHub de markdown (estilo Supermemory).
- **Grafo de entidades e relações** extraídas de documentos e memórias.

Uso single-user. Roda 24/7 em VPS via Docker. Todas as capacidades fazem parte da **primeira entrega** (sem faseamento — decisão explícita do autor).

---

## 2. Decisões de arquitetura (resumo)

| Dimensão | Decisão |
|---|---|
| Integração | Servidor **MCP** (transporte HTTP/SSE), auth por **bearer token** |
| Stack | **Python** |
| IA | **OpenAI** — GPT (extração de fatos/entidades) + `text-embedding-3-large` com `dimensions=2000` (habilita índice HNSW; perda de qualidade negligenciável via Matryoshka) |
| Armazenamento | **Postgres tudo-em-um**: `pgvector` (vetorial) + **Apache AGE** (grafo) + tabelas/jsonb (metadados, fila) |
| Documentos | Fonte da verdade = **repo GitHub de `.md`**; provedor reindexa via **webhook → `git pull` → diff** |
| Fatos de conversas | Extraídos por LLM, guardados no **Postgres** (camada separada; busca unificada com documentos) |
| Histórico bruto | Persistido como nota **`.md` no repo**; **o brain grava** (commit/push) |
| Fila | Interface **`JobQueue` abstrata** + backend **Postgres (`SKIP LOCKED`)**; adaptadores Redis/RabbitMQ documentados como evolução |
| Execução | VPS, Docker, 24/7. Mesma imagem em dois papéis: **`api`** e **`worker`** + **`postgres`** |
| Nome | **brain** |

### 2.1 Abordagem escolhida: Monolito modular (A)

Um serviço Python com módulos internos de fronteira clara, empacotado em **uma imagem** que sobe em dois papéis (API e worker), ao lado do Postgres. Mantém operação simples (3 containers, 1 imagem de app) e já isola a carga de ingestão da carga de busca. Migração para fila externa (Redis/RabbitMQ) ou worker totalmente separado é troca de adaptador, não reescrita.

**Alternativas descartadas:** (B) serviço + worker + Redis/RabbitMQ desde já — over-engineering para 1 usuário; (C) camada fina sobre framework pronto (LlamaIndex/Mem0) — retira o controle do grafo e da curadoria, que são o diferencial.

---

## 3. Diagrama de alto nível

```
                    ┌─────────────────────────────────────────┐
   GitHub push ───▶ │  webhook  →  git pull + diff  →  enqueue │
                    └─────────────────────────────────────────┘
                                       │ (Postgres job queue)
   Agentes (MCP) ──┐                   ▼
                   │            ┌──────────────┐
   search/remember │            │    WORKER    │  chunk · embed ·
   get/move/delete │            │ (ingestion)  │  extrair fatos/entidades
                   ▼            └──────┬───────┘
            ┌──────────────┐          │
            │  API + MCP   │          │
            │  (FastAPI)   │          ▼
            └──────┬───────┘   ┌──────────────────────────────┐
                   └──────────▶│  POSTGRES                     │
                               │  pgvector · Apache AGE · jsonb│
                               └──────────────────────────────┘
```

---

## 4. Componentes (módulos internos)

| Módulo | Responsabilidade única | Depende de |
|---|---|---|
| `mcp/` | Servidor MCP (HTTP/SSE) + tools + auth por token | search, storage, queue |
| `main.py` (API) | FastAPI: monta MCP, recebe webhook GitHub, healthcheck | git_sync, queue |
| `worker.py` | Loop que consome jobs da fila e roda o pipeline | queue, ingestion |
| `ingestion/git_sync.py` | `git pull`, calcula diff de arquivos alterados, enfileira | queue |
| `ingestion/git_writer.py` | Escreve `.md` de conversa, commit (autor `brain-bot`), push com retry | — |
| `ingestion/pipeline.py` | Orquestra chunk → embed → extrair → gravar | indexing, extraction, graph, storage |
| `extraction/` | LLM OpenAI: fatos de conversas + entidades/relações de docs | OpenAI |
| `indexing/` | Chunking + embeddings (`text-embedding-3-large`) | OpenAI |
| `graph/age.py` | Grava/consulta entidades e relações no Apache AGE | storage |
| `search/retriever.py` | Busca unificada: vetorial + expansão por grafo + ranking | storage, graph |
| `queue/` | Interface `JobQueue` + `PostgresJobQueue` (SKIP LOCKED) | storage |
| `storage/` | SQLAlchemy + repositórios + migrations (Alembic) | Postgres |
| `auth.py` | Validação de bearer token | — |

### 4.1 Estrutura de projeto

```
brain/
  pyproject.toml
  Dockerfile                 # imagem da app (api + worker)
  docker/postgres/Dockerfile # postgres + pgvector + AGE
  docker-compose.yml         # api · worker · postgres
  src/brain/
    main.py  worker.py  config.py  auth.py
    mcp/        (server.py, tools/)
    ingestion/  (git_sync.py, git_writer.py, pipeline.py)
    extraction/ (facts.py, entities.py, llm.py)
    indexing/   (chunker.py, embeddings.py)
    graph/      (age.py)
    search/     (retriever.py)
    queue/      (base.py, postgres_queue.py)
    storage/    (db.py, models.py, repositories.py)
  migrations/
  tests/
```

---

## 5. Modelo de dados (Postgres)

**Camada de documentos** (espelho indexado do repo GitHub):
- `documents` — `id`, `namespace`, `repo_path`, `title`, `raw_content`, `content_hash`, `commit_sha`, `created_at`, `updated_at`
- `chunks` — `id`, `document_id`, `ordinal`, `text`, `embedding vector(2000)`, `token_count`

**Camada de memórias** (fatos extraídos de conversas):
- `memories` — `id`, `namespace`, `content`, `kind` (`fact`), `source` (`conversation`), `embedding vector(2000)`, `confidence`, `supersedes_id`, `metadata jsonb`, `created_at`, `updated_at`

**Grafo** (Apache AGE — entidades e relações extraídas de docs e memórias):
- vértices `entity` — `name`, `type` (pessoa/projeto/conceito…), `namespace`, `props`, proveniência (de qual `document`/`memory` veio)
- arestas `relation` — `type` (ex: `works_on`, `related_to`), `props`

**Infra:**
- `ingestion_jobs` — `id`, `type` (`index_document` | `extract_facts` | `reindex`), `payload jsonb`, `status` (`pending`/`running`/`done`/`failed`), `attempts`, `locked_at`, `locked_by`, `created_at` — fila durável (SKIP LOCKED)
- `namespaces` — `name`, `description`

**Nota:** `chunks` e `memories` têm `embedding vector(2000)`, então a busca varre as duas camadas e devolve resultado unificado, com a origem marcada (documento vs. fato). Cada coluna de embedding recebe um índice **HNSW** (`vector_cosine_ops`) — possível porque 2000 ≤ 2000, o limite do pgvector para índices ANN.

---

## 6. Fluxos de dados

### A) Ingestão de documentos (assíncrono, idempotente)
```
push no repo → webhook (valida HMAC) → git pull → diff (arquivos alterados)
  → enfileira index_document por arquivo (dedupe por content_hash)
  → WORKER: chunk → embed → extrair entidades/relações → grava documents+chunks+grafo
```
- Idempotência: `content_hash` inalterado → no-op.
- Arquivo deletado no repo → remove `document` + `chunks` + entidades órfãs.
- Ignora commits do autor `brain-bot` (anti-loop).

### B) Memória / extração de fatos (assíncrono)
```
agente chama tool remember(namespace, mensagens, metadata?)
  → git_writer grava conversas/{namespace}/{timestamp}-{slug}.md (commit brain-bot + push, retry em non-fast-forward)
  → enfileira extract_facts + index_document da própria nota
  → WORKER: LLM extrai fatos → dedupe/consolidação (supersedes) → embed → grava memories (+entidades no grafo)
```
- Recall disponível quando o pipeline termina.

### C) Busca / recall (síncrono, rápido)
```
agente chama search(query, namespace?, filtros?, include_graph?)
  → embed da query → busca vetorial (chunks + memories)
  → expansão opcional por grafo (entidades relacionadas)
  → ranking unificado → retorna trechos + proveniência + score
```

### D) Gerenciamento (síncrono — base para o Hermes curar depois)
```
list / get / update / move(namespace) / delete / merge — em memories e entities
```

---

## 7. API MCP (tools)

Todas exigem **bearer token** (header `Authorization`). Transporte HTTP/SSE.

**Memória & recall**
| Tool | Faz |
|---|---|
| `remember(namespace, messages, metadata?)` | Grava `.md` bruto (commit/push) + extrai fatos + indexa. Retorna ids das memórias e o caminho da nota |
| `search(query, namespace?, filters?, limit?, include_graph?)` | Busca unificada (chunks + memories), retorna trechos + proveniência + score |
| `get_memory(id)` / `list_memories(namespace?, filters?)` | Leitura |

**Documentos**
| Tool | Faz |
|---|---|
| `get_document(id_or_path)` / `list_documents(namespace?)` | Leitura |
| `reindex(path?)` | Força reindexação (recuperação manual) |

**Grafo**
| Tool | Faz |
|---|---|
| `get_entity(name, namespace?)` | Entidade + props |
| `search_entities(query, namespace?)` | Busca entidades |
| `get_related(entity, depth=1, namespace?)` | Vizinhança no grafo (multi-hop) |

**Gerenciamento (base para o Hermes)**
| Tool | Faz |
|---|---|
| `update_memory` / `move_memory` / `delete_memory` | Editar, mover de namespace, remover |
| `merge_memories(ids[], into?)` | Consolidar duplicatas |
| `update_entity` / `merge_entities` / `delete_entity` | Curadoria do grafo |

**Namespaces**
| Tool | Faz |
|---|---|
| `list_namespaces()` / `create_namespace(name, description?)` | Organização por contexto |

`health`/`status` ficam como endpoints **HTTP** (não MCP), para Docker/monitoramento.

---

## 8. Tratamento de erros (sem falha silenciosa)

- **Webhook:** valida assinatura HMAC (`WEBHOOK_SECRET`), responde rápido e enfileira. Idempotente por `content_hash`; ignora commits do autor `brain-bot`.
- **Jobs:** retry com backoff exponencial; após N tentativas (padrão 5) → `failed` (dead-letter), logado e inspecionável via `/status`. Erro nunca é engolido.
- **OpenAI:** timeout/rate-limit → retry com backoff; se persistir, job `failed` (dado não se perde — pode `reindex`).
- **Git push:** `non-fast-forward` → `pull --rebase` + retry limitado; se falhar, job `failed` + log de alerta.
- **Tools MCP:** validação de input com Pydantic; erros estruturados retornados ao agente.
- **Embeddings:** chunk grande demais → split; falha parcial não derruba o documento inteiro.

---

## 9. Testes (TDD)

- **Unit:** chunker, ranking de busca, `JobQueue` (concorrência SKIP LOCKED), `git_writer` (mock), extração (LLM **mockado**), auth.
- **Integração:** Postgres real via **testcontainers** — pgvector (busca), AGE (grafo), fila (claim concorrente).
- **E2E:** webhook → indexa → `search` retorna; `remember` → grava `.md` + extrai → `search` acha o fato; `delete`/`move`/`merge`.
- **Determinismo:** mockar OpenAI nos testes (respostas fixas); testes de contrato para o formato JSON de extração.

---

## 10. Deploy / operação

- `docker-compose.yml`: serviços **`api`**, **`worker`** (mesma imagem `brain`), **`postgres`** (imagem custom pgvector+AGE).
- **Migrations** com Alembic (no startup do `api` ou job de init).
- **Secrets** por env: `OPENAI_API_KEY`, `GITHUB_TOKEN`, `BRAIN_AUTH_TOKEN`, `WEBHOOK_SECRET`, `DATABASE_URL`, `REPO_URL`.
- **Backup:** `pg_dump` agendado (um banco só).
- **TLS:** reverse proxy (Caddy/Traefik) na frente do endpoint MCP HTTP.
- **Observabilidade:** logs estruturados (structlog), endpoints `/health` e `/status` (jobs pendentes/failed).

---

## 11. Riscos / decisões em aberto

1. **Imagem Postgres custom** (pgvector + AGE): não há imagem oficial com os dois. **Mitigação adotada:** multi-stage build a partir de `pgvector/pgvector:pg16`, compilando o Apache AGE numa **release fixada** (`release/PG16/1.5.0`) num estágio builder e copiando os artefatos para a imagem final — reprodutível e sob controle. Validada antes de tudo (primeira task do plano).
2. **Qualidade da extração** de fatos/entidades: depende de prompt + schema JSON validado de saída.
3. **Chunking de markdown:** estratégia proposta = split por headings + sub-split por tamanho com overlap.
4. **Concorrência de push no Git:** baixa para 1 usuário, mas tratada (pull --rebase + retry).
5. **Custo OpenAI** (`text-embedding-3-large` + extração): aceitável para uso pessoal; cache (Redis) é evolução natural se incomodar.

---

## 12. Caminho de evolução (fora deste spec)

- Adaptadores `RedisQueue` / `RabbitMQQueue` plugáveis na interface `JobQueue` (quando o volume justificar).
- Worker totalmente separado em container/host próprio (modelo B).
- Cache de embeddings/queries (Redis) para reduzir custo OpenAI.
- **Agente curador Hermes** (projeto separado) consumindo as tools de gerenciamento deste provedor.
