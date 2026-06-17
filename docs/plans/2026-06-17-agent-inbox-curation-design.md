# Brain — Inbox de Agentes e Curadoria Hermes

## Objetivo

Substituir a semantica atual de `remember` por um fluxo em que clientes
LLMs/agentes enviam notas brutas para o `brain`, e o Hermes faz a curadoria
antes de qualquer conteudo virar conhecimento pesquisavel.

O `brain` continua sendo o servidor MCP, armazenamento, indexador e ponto unico
de escrita no GitHub. Clientes nao escrevem direto no repositorio.

## Principios

- Clientes criam apenas material bruto.
- O Hermes e o unico ator que le notas brutas e cria conhecimento curado.
- `search` e `get_note` retornam apenas conhecimento curado.
- `_agents/` e inbox bruta, privada para curadoria.
- Fora de `_agents/` ficam as notas curadas.
- O `brain` nao impoe estrutura de pastas para notas curadas.
- Tokens de clientes sao exclusivos, recuperaveis e criptografados no Postgres.
- Tokens completos nunca sao gravados no GitHub.

## Bootstrap e Autenticacao

O curador nao e um client. Ele e configurado por variaveis de ambiente:

```env
BRAIN_CURATOR_SLUG=hermes
BRAIN_CURATOR_NAME=Hermes
BRAIN_CURATOR_TOKEN=...
BRAIN_TOKEN_ENCRYPTION_KEY=...
```

Todos autenticam pelo mesmo header:

```http
Authorization: Bearer <token>
```

O `brain` resolve o principal assim:

1. Se o token bater com `BRAIN_CURATOR_TOKEN`, o principal e
   `curator/hermes`.
2. Caso contrario, o `brain` procura o hash do token em `agent_clients`.
3. Se encontrar client ativo, o principal e `client/{slug}`.
4. Caso contrario, retorna `401`.

## Clientes

Clientes sao LLMs/agentes como ChatGPT web, Codex, Claude, Cursor ou agentes
proprios.

Um client so existe depois que Hermes chama `create_agent_client`. O `brain`
gera um token exclusivo, salva hash + token criptografado no Postgres, cria a
pasta do client em `_agents/{client_slug}/` e grava um perfil sem segredo em:

```text
_agents/{client_slug}/{client_slug}.md
```

O perfil e gerado pelo `brain` a partir de campos estruturados fornecidos pelo
Hermes:

```text
create_agent_client(
  name,
  slug?,
  description,
  capture_policy,
  recommended_instructions,
  metadata?
)
```

O token completo pode ser revelado depois apenas pelo Hermes via
`reveal_agent_client_token`.

## Tools por Papel

### Cliente

```text
search
get_note
submit_agent_note
```

`search` busca somente conhecimento curado.

`get_note` abre uma nota curada especifica descoberta via `search`.

`submit_agent_note` envia material bruto. O client nao informa `client` nem
`path`; o `brain` identifica o client pelo token e grava em:

```text
_agents/{client_slug}/{yyyy}/{mm}/{dd}/{timestamp}-{slug}.md
```

### Hermes

Gestao de clients:

```text
create_agent_client
list_agent_clients
get_agent_client
reveal_agent_client_token
rotate_agent_client_token
disable_agent_client
```

Curadoria de notas brutas:

```text
list_agent_notes
get_agent_note
claim_agent_note
complete_agent_note
reject_agent_note
fail_agent_note
```

Notas curadas:

```text
list_vault_tree
create_note
update_note
get_note
search
```

Links entre notas:

```text
list_unresolved_links
resolve_note_link
```

## submit_agent_note

Assinatura:

```text
submit_agent_note(
  title?,
  content?,
  messages?,
  suggested_namespace?,
  metadata?
)
```

Regras:

- Precisa receber `content` ou `messages`.
- O client nao envia `client`.
- O client nao envia `path`.
- O `brain` salva o arquivo no GitHub.
- O `brain` registra a nota como `pending` no Postgres.
- O `brain` cria evento duravel `agent_note.created` para Hermes.
- O corpo da nota e livre. Cada client pode usar o formato que preferir.
- Se `messages` vierem, o `brain` renderiza como Markdown simples.

Envelope minimo da nota bruta:

```md
---
type: "agent_note"
id: "agent_note_..."
client_slug: "chatgpt-web"
client_name: "ChatGPT Web"
created_at: "2026-06-17T18:30:00Z"
suggested_namespace: "brain"
metadata:
  model: "gpt-5.5"
---

# Titulo opcional

Conteudo livre enviado pelo client.
```

Se houver `messages`, elas sao adicionadas como texto Markdown simples:

```md
**user:** ...

**assistant:** ...
```

## Estados

Estado da nota bruta, controlado pelo Hermes via MCP:

```text
pending
in_review
curated
rejected
failed
```

Estado do evento outbox, controlado internamente pelo `brain`:

```text
pending
retrying
delivered
failed
```

`claim_agent_note` e opcional. Hermes pode chamar `complete_agent_note` ou
`reject_agent_note` diretamente.

`complete_agent_note(note_id, outcome?)` aceita um objeto flexivel para
auditoria, por exemplo:

```json
{
  "created_notes": ["projetos/brain.md"],
  "updated_notes": ["decisoes/inbox-agentes.md"],
  "summary": "Nota bruta usada para registrar a arquitetura de inbox."
}
```

## Webhook para Hermes

Quando uma nota bruta e criada, o `brain` grava um evento outbox e tenta
entregar para Hermes.

Payload:

```json
{
  "event_id": "evt_...",
  "event_type": "agent_note.created",
  "created_at": "2026-06-17T18:30:00Z",
  "attempt": 1,
  "agent_note": {
    "id": "agent_note_...",
    "client_slug": "chatgpt-web",
    "client_name": "ChatGPT Web",
    "repo_path": "_agents/chatgpt-web/2026/06/17/resumo.md",
    "title": "Resumo antes da compressao",
    "suggested_namespace": "brain",
    "metadata": {
      "model": "gpt-5.5"
    }
  }
}
```

O webhook nao envia o conteudo completo da nota. Hermes chama
`get_agent_note(note_id)` pelo MCP.

Headers:

```http
X-Brain-Event-Id: evt_...
X-Brain-Event-Type: agent_note.created
X-Brain-Signature: sha256=<hmac>
X-Brain-Timestamp: 2026-06-17T18:30:00Z
```

Assinatura:

```text
HMAC_SHA256(HERMES_WEBHOOK_SECRET, timestamp + "." + raw_body)
```

Se Hermes responder `2xx`, o `brain` marca o evento como `delivered`. Isso
significa apenas que Hermes recebeu o aviso, nao que a nota foi curada.

Hermes deve iniciar a curadoria automaticamente ao receber o webhook.

## Notas Curadas

Hermes cria e atualiza notas curadas com:

```text
create_note(path, content, metadata?, source_agent_note_ids?)
update_note(id_or_path, content, metadata?, source_agent_note_ids?)
```

Regras:

- `path` e relativo ao repo.
- `path` nao pode comecar com `_agents/`.
- `path` nao pode conter `..`.
- `path` deve terminar em `.md`.
- Pastas pais sao criadas automaticamente.
- `create_note` falha se a nota ja existir.
- `update_note` substitui a nota inteira.
- Hermes envia o Markdown completo, incluindo `# Titulo`.
- O `brain` preserva `id` e `created_at` em updates.
- O `brain` atualiza `updated_at`.
- Metadados ficam no frontmatter e no Postgres.
- O `brain` indexa a nota para `search`.

Exemplo:

```json
{
  "path": "projetos/brain.md",
  "content": "# Brain\n\nO [[brain]] usa [[MCP]] para expor memoria aos agentes.",
  "metadata": {
    "tags": ["projeto", "ia"]
  },
  "source_agent_note_ids": ["agent_note_..."]
}
```

## Busca e Leitura

`search(query, limit=10, filters=null)`:

- Busca sempre somente conhecimento curado.
- Nunca retorna notas em `_agents/`.
- Clientes decidem se usam filtros ou nao.
- Retorna trechos, score, id e path.

`get_note(id_or_path)`:

- Abre somente nota curada.
- Nunca abre `_agents/...`.
- Normalmente e usado depois de `search`.

## Estrutura do Vault

No inicio, a unica pasta obrigatoria e:

```text
_agents/
```

O Hermes cria as demais pastas implicitamente ao chamar `create_note` com paths
novos:

```text
projetos/brain.md
decisoes/arquitetura/inbox-de-agentes.md
```

Nao existe tool `create_folder`; Git nao versiona pastas vazias. Pastas sao
criadas automaticamente ao criar notas.

Hermes pode consultar a estrutura existente com:

```text
list_vault_tree(prefix?, include_agents=false, max_depth?)
```

Por padrao, `_agents/` fica fora do resultado.

## Links entre Notas

Links entre notas curadas usam sintaxe Obsidian:

```md
[[MCP]]
[[projetos/brain]]
[[projetos/brain|Brain]]
[[Hermes#Curadoria]]
```

Responsabilidades:

- Hermes decide quais links criar no Markdown.
- O `brain` preserva os links.
- O `brain` extrai links apenas de notas curadas.
- Notas brutas nao geram links.
- Links quebrados sao permitidos.
- Links extraidos ficam em `note_links`.

Se o alvo existir, o `brain` pode resolver `target_path`. Se nao existir, o
link fica pendente.

Hermes usa:

```text
list_unresolved_links(limit?, cursor?)
resolve_note_link(link_id, target_path)
```

`resolve_note_link` exige que `target_path` exista e nao esteja em `_agents/`.
O Markdown original pode continuar como escrito; a resolucao estrutural fica no
Postgres. Hermes pode atualizar o Markdown depois se quiser.
