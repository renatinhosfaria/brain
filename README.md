# brain — Inbox de agentes e notas curadas (MCP)

Servidor MCP para um vault Markdown curado. Clientes de agente pesquisam apenas
notas curadas, leem notas curadas por id ou caminho e enviam notas brutas para o
inbox `_agents/`. O Hermes atua como curador: revisa o inbox, cria ou atualiza
notas curadas e decide o que entra na busca do vault.

## Subir em produção (VPS)

```bash
cp .env.example .env   # preencha os segredos
docker compose build   # compila Postgres (pgvector+AGE) e a app
docker compose up -d
curl http://localhost:8000/health   # -> {"status":"ok","database":"ok"}
```

A aplicação recusa os placeholders da `.env.example` no startup. Troque pelo
menos `DATABASE_URL`, `OPENAI_API_KEY`, `GITHUB_TOKEN`, `BRAIN_CURATOR_TOKEN`,
`BRAIN_TOKEN_ENCRYPTION_KEY`, `WEBHOOK_SECRET` e `REPO_URL` antes de subir em
produção.

Gere `BRAIN_TOKEN_ENCRYPTION_KEY` com uma chave Fernet:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Para publicar com TLS via Caddy, aponte o DNS de `BRAIN_DOMAIN` para a VPS,
preencha `BRAIN_DOMAIN` no `.env` e suba o profile do proxy:

```bash
docker compose --profile proxy up -d
curl https://SEU_DOMINIO/health
```

Por padrão o Caddy publica `80` e `443`. Se já houver outro proxy no host,
ajuste `BRAIN_HTTP_PORT` e `BRAIN_HTTPS_PORT` no `.env`.

O endpoint `/health` é público para healthchecks. O endpoint `/status` exige o
token de curador:

```bash
curl -H "Authorization: Bearer $BRAIN_CURATOR_TOKEN" https://SEU_DOMINIO/status
```

Para backups agendados do Postgres, suba também o profile `backup`:

```bash
docker compose --profile backup up -d backup
```

Os dumps ficam no volume `backups` em formato custom do `pg_dump` (`.dump`).
Use `BRAIN_BACKUP_INTERVAL_SECONDS` e `BRAIN_BACKUP_RETENTION_DAYS` para
ajustar frequência e retenção. Para testar um backup único:

```bash
docker compose --profile backup run --rm -e BRAIN_BACKUP_ONCE=true backup
```

## Configurar o webhook do GitHub

No repositório do vault: Settings → Webhooks → Add webhook.
- Payload URL: `https://SEU_DOMINIO/webhook/github`
- Content type: `application/json`
- Secret: o mesmo valor de `WEBHOOK_SECRET`
- Eventos: apenas `push`

O webhook do GitHub e o worker indexam apenas notas curadas. Arquivos em
`_agents/` são ignorados pela indexação, mesmo quando chegam por push.

## Bootstrap do Hermes

O curador é o Hermes, não um cliente de agente. Configure:

```dotenv
BRAIN_CURATOR_SLUG=hermes
BRAIN_CURATOR_NAME=Hermes
BRAIN_CURATOR_TOKEN=...
```

O Hermes conecta no MCP com o token de bootstrap vindo do ambiente:

```text
Endpoint: https://SEU_DOMINIO/mcp
Authorization: Bearer <BRAIN_CURATOR_TOKEN>
```

`BRAIN_AUTH_TOKEN` é apenas fallback de migração enquanto
`BRAIN_CURATOR_TOKEN` estiver ausente. Depois que `BRAIN_CURATOR_TOKEN` é
configurado, o Hermes deve usar esse token; `BRAIN_AUTH_TOKEN` não funciona como
credencial paralela de curador.

## Criar e configurar clientes

Endpoint: `https://SEU_DOMINIO/mcp` (transporte streamable HTTP).

1. Hermes chama `create_agent_client` com nome, slug opcional, descrição,
   política de captura e instruções recomendadas.
2. O brain cria o perfil `_agents/{slug}/{slug}.md` no vault.
3. O token retornado é exclusivo do cliente. O hash é usado para autenticar, e
   o token completo fica criptografado de forma recuperável no Postgres com
   `BRAIN_TOKEN_ENCRYPTION_KEY`.
4. Configure o cliente MCP com:

```text
Endpoint: https://SEU_DOMINIO/mcp
Authorization: Bearer <TOKEN_DO_CLIENTE>
```

Use `reveal_agent_client_token` para recuperar um token existente e
`rotate_agent_client_token` para trocar o segredo de um cliente.

## Ferramentas MCP de inbox e curadoria

Clientes de agente têm acesso limitado:
- `search`: busca semântica apenas em notas curadas.
- `get_note`: lê uma nota curada por id ou caminho.
- `submit_agent_note`: envia uma nota bruta em Markdown ou `messages`.

Hermes tem acesso de curadoria:
- Clientes: `create_agent_client`, `list_agent_clients`,
  `get_agent_client`, `reveal_agent_client_token`,
  `rotate_agent_client_token`, `disable_agent_client`.
- Notas brutas: `list_agent_notes`, `get_agent_note`, `claim_agent_note`,
  `complete_agent_note`, `reject_agent_note`, `fail_agent_note`.
- Notas curadas: `create_note`, `update_note`, `get_note`, `search`.
- Vault: `list_vault_tree`; por padrão oculta `_agents/`, mas Hermes pode usar
  `include_agents=true` para inspeção operacional.
- Links: `list_unresolved_links` e `resolve_note_link`.

Esta lista destaca o fluxo de inbox e curadoria. Ferramentas técnicas de
documentos, reindexação e grafo de entidades, como `get_document`,
`list_documents`, `reindex`, `get_entity`, `search_entities`, `get_related`,
`update_entity`, `merge_entities` e `delete_entity`, continuam expostas apenas
ao curador para compatibilidade e operações.

## Notas brutas e notas curadas

Notas brutas de clientes ficam em `_agents/{slug}/...` e representam inbox de
curadoria. Clientes não conseguem ler essas notas brutas pelo MCP.

Notas curadas são criadas fora de `_agents/` com `create_note` e atualizadas
com `update_note`. Só notas curadas entram na busca, em `get_note` para
clientes, e na resolução de links do vault.

Quando um cliente chama `submit_agent_note`, o brain grava a nota em `_agents/`,
cria um evento `agent_note.created` no outbox e faz push se
`GIT_PUSH_ENABLED=true`. O evento é apenas uma referência: id da nota, slug do
cliente e caminho do arquivo. O conteúdo continua no vault/Postgres e não é
enviado no webhook.

`_agents/` não é uma barreira de confidencialidade. Notas brutas podem conter
contexto sensível; mesmo sem indexação e sem exposição para clientes, elas são
commits no vault e podem ser enviadas para `REPO_URL` quando
`GIT_PUSH_ENABLED=true`. Mantenha o repositório do vault privado e restrito aos
operadores que podem ver esse inbox.

## Webhook para Hermes

Configure:

```dotenv
HERMES_WEBHOOK_URL=https://hermes.exemplo/events
HERMES_WEBHOOK_SECRET=...
```

O worker entrega eventos pendentes do outbox para `HERMES_WEBHOOK_URL` com
assinatura HMAC em `X-Brain-Signature`, tipo em `X-Brain-Event-Type`, id em
`X-Brain-Event-Id` e timestamp em `X-Brain-Timestamp`. Falhas são persistidas e
retentadas com backoff até o limite de tentativas configurado.

## Exemplo de instruções para um cliente

```text
You are connected to brain as an agent client.

Use search before answering when stored context may help. Read curated notes
with get_note when a search result looks relevant.

Before context compression, or when the conversation has accumulated important
decisions, tasks, preferences, or high context usage, call submit_agent_note.
Send plain Markdown in content, or plain role/content objects in messages. Do
not send HTML, proprietary formatting, hidden chain-of-thought, secrets, or
binary data.

Raw submissions are inbox notes for Hermes. Do not assume they are searchable
until Hermes turns them into curated notes.
```

## Desenvolvimento

```bash
docker build -t brain-postgres:local docker/postgres   # imagem usada pelos testes
uv sync
uv run pytest
```
