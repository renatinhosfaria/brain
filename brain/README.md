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
