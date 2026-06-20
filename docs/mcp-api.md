# Referência MCP

## Endpoint E Transporte

O endpoint MCP é `/mcp`.

O transporte é HTTP streamable fornecido pelo `FastMCP`. As requisições MCP devem incluir o cabeçalho:

```http
Authorization: Bearer brain_client_exemplo_123456
```

Use `Authorization: Bearer <token>` em integrações reais. O valor `brain_client_exemplo_123456` é apenas um exemplo não secreto.

O endpoint `/status` não usa autenticação MCP. Ele é protegido separadamente por `BRAIN_AUTH_TOKEN` e não aceita tokens de cliente ou curador como principal MCP.

## Autenticação E Principals

O principal `curator` é inicializado por `BRAIN_CURATOR_TOKEN`. Esse token identifica operações de curadoria, administração, ciclo de vida de clientes e manutenção do grafo. Quando aparecer como valor de exemplo, `Hermes` representa a identidade de curador.

O principal `client` é criado pelas ferramentas de curadoria de clientes. Clientes autenticam com um token emitido pelo sistema; o servidor resolve o principal comparando o hash armazenado do token recebido.

Tokens bearer ausentes, vazios, inválidos ou desabilitados recebem `401`.

O contexto de principal é definido pelo middleware `brain.auth` antes que os handlers MCP executem. Assim, cada ferramenta consulta o principal ativo e aplica seus guards de permissão dentro de `brain.mcp.handlers`.

## Matriz De Ferramentas

| Ferramenta | Cliente | Curador | Observação |
| --- | --- | --- | --- |
| `search` | sim | sim | Busca semântica em documentos curados. |
| `deep_search` | sim | sim | Busca semântica curada com contexto do grafo. |
| `get_note` | sim | sim | Lê notas curadas; não expõe `_agents/` para clientes. |
| `submit_agent_note` | sim | não | Envio de nota bruta por cliente ativo com permissão explícita. |
| `create_note` | não | sim | Cria nota curada no vault. |
| `update_note` | não | sim | Atualiza nota curada no vault. |
| `list_vault_tree` | não | sim | Lista árvore do vault; `_agents/` exige opção administrativa. |
| `list_unresolved_links` | não | sim | Lista links pendentes de resolução. |
| `resolve_note_link` | não | sim | Resolve link para um caminho de nota. |
| `create_agent_client` | não | sim | Cria cliente e retorna o token gerado uma única vez. |
| `list_agent_clients` | não | sim | Lista clientes de agentes. |
| `get_agent_client` | não | sim | Obtém perfil de um cliente. |
| `reveal_agent_client_token` | não | sim | Revela token criptografado armazenado, se configurado. |
| `rotate_agent_client_token` | não | sim | Gera novo token de cliente. |
| `disable_agent_client` | não | sim | Desativa cliente. |
| `list_agent_notes` | não | sim | Lista notas brutas enviadas por clientes. |
| `get_agent_note` | não | sim | Lê uma nota bruta de agente. |
| `claim_agent_note` | não | sim | Marca nota bruta como reivindicada. |
| `complete_agent_note` | não | sim | Marca nota bruta como concluída. |
| `reject_agent_note` | não | sim | Rejeita nota bruta. |
| `fail_agent_note` | não | sim | Marca falha no processamento de nota bruta. |
| `get_document` | não | sim | Consulta documento indexado. |
| `list_documents` | não | sim | Lista documentos indexados. |
| `reindex` | não | sim | Enfileira reindexação de documento. |
| `get_entity` | não | sim | Consulta entidade do grafo. |
| `search_entities` | não | sim | Busca entidades do grafo. |
| `get_related` | não | sim | Lista relações de uma entidade. |
| `update_entity` | não | sim | Atualiza propriedades de entidade. |
| `merge_entities` | não | sim | Mescla entidades no grafo. |
| `delete_entity` | não | sim | Remove entidade do grafo. |

## Ferramentas De Cliente

### `search`

```text
search(query: str, limit: int = 10, filters: dict | None = None) -> dict
```

Propósito: executar busca semântica sobre chunks de documentos curados.

Parâmetros de entrada:

- `query`: texto da busca.
- `limit`: quantidade solicitada de resultados; o repositório normaliza o valor final.
- `filters`: filtros opcionais aceitos pelo retriever.

Formato de saída: dicionário com `results` e `graph`. Os resultados curados normalmente contêm `id`, `source`, `ref`, `path`, `repo_path`, `namespace`, `text` e `score`. A chave `graph` é mantida por compatibilidade; pela API MCP pública, `include_graph` não é exposto e ela normalmente vem como lista vazia.

Permissões: disponível para `client` e `curator`.

Limites de fronteira: clientes recebem apenas conteúdo curado indexado. Notas brutas em `_agents/` não fazem parte da superfície de busca de cliente.

### `deep_search`

```text
deep_search(query: str, limit: int = 10, depth: int = 1, max_entities: int = 3, rel_types: list[str] | None = None, filters: dict | None = None, namespace: str | None = None) -> dict
```

Propósito: combinar busca semântica sobre conteúdo curado com contexto do grafo para retornar documentos, entidades relacionadas e metadados da expansão.

Parâmetros de entrada:

- `query`: texto da busca.
- `limit`: quantidade solicitada de resultados semânticos; o repositório normaliza o valor final.
- `depth`: profundidade de expansão do grafo, de 1 a 3.
- `max_entities`: número máximo de entidades de partida, de 1 a 3.
- `rel_types`: lista opcional de tipos de relação; lista vazia equivale a filtro ausente.
- `filters`: filtros opcionais para a busca semântica.
- `namespace`: namespace opcional para limitar a consulta no grafo.

Formato de saída: dicionário com `query`, `results`, `graph.entities`, `graph.relationships` e `meta`.

Permissões: disponível para `client` e `curator`.

Limites de fronteira: a busca semântica continua restrita a documentos curados. O `namespace`, quando informado, limita a consulta ao grafo; ele não transforma `_agents/` em fonte de conteúdo para clientes.

### `get_note`

```text
get_note(id_or_path: str) -> dict | None
```

Propósito: recuperar uma nota curada por identificador ou caminho.

Parâmetros de entrada:

- `id_or_path`: UUID do documento curado ou caminho relativo no repositório, como `projetos/brain.md`.

Formato de saída: dicionário com dados da nota curada, incluindo `id`, `path`, `repo_path`, `title`, `content`, `metadata` e `source_agent_note_ids`; retorna `null` quando não encontrar.

Permissões: disponível para `client` e `curator`.

Limites de fronteira: clientes podem ler notas curadas. Caminhos de `_agents/` são área de captura e revisão, não contrato de leitura para clientes.

### `submit_agent_note`

```text
submit_agent_note(title: str | None = None, content: str | None = None, messages: list[dict] | None = None, suggested_namespace: str | None = None, metadata: dict | None = None) -> dict
```

Propósito: permitir que um cliente envie uma nota bruta para curadoria posterior.

Parâmetros de entrada:

- `title`: título opcional da nota.
- `content`: conteúdo textual opcional; obrigatório quando `messages` não for informado.
- `messages`: lista opcional de mensagens estruturadas; obrigatória quando `content` não for informado.
- `suggested_namespace`: namespace sugerido para a curadoria.
- `metadata`: metadados opcionais do cliente.

Formato de saída: dicionário com `note_id`, `repo_path`, `status` e `event_id`.

`client_slug`, `metadata` e timestamps da nota são persistidos nos dados da nota de agente e do outbox. Esses campos ficam visíveis em fluxos de curadoria como `get_agent_note` e `list_agent_notes`, mas não são retornados diretamente por `submit_agent_note`.

Permissões: disponível apenas para `client`; além do principal de cliente, o cliente ativo precisa ter a permissão `submit_agent_note`.

Limites de fronteira: a ferramenta grava em área de inbox de agentes, normalmente sob `_agents/`, e não cria uma nota curada automaticamente. A promoção para conteúdo curado é responsabilidade das ferramentas de curadoria.

## Ferramentas De Curadoria

Ciclo de vida de clientes:

- `create_agent_client`: cria um cliente de agente com permissões padrão `search`, `deep_search`, `get_note` e `submit_agent_note`; retorna o token uma única vez.
- `list_agent_clients`: lista clientes cadastrados.
- `get_agent_client`: obtém os dados de um cliente por slug.
- `reveal_agent_client_token`: revela o token criptografado armazenado quando a chave de criptografia está configurada.
- `rotate_agent_client_token`: substitui o token de um cliente.
- `disable_agent_client`: desativa um cliente.

Ciclo de vida de notas brutas de agente:

- `list_agent_notes`: lista notas brutas enviadas por clientes, com filtros por status e cliente.
- `get_agent_note`: lê uma nota bruta.
- `claim_agent_note`: marca uma nota bruta como em atendimento.
- `complete_agent_note`: marca uma nota bruta como concluída, com resultado opcional.
- `reject_agent_note`: rejeita uma nota bruta com motivo opcional.
- `fail_agent_note`: registra falha de processamento.

Ciclo de vida de notas curadas:

- `create_note`: cria uma nota curada.
- `update_note`: atualiza uma nota curada existente.

Árvore do vault e resolução de links:

- `list_vault_tree`: lista caminhos do vault curado e pode incluir `_agents/` em uso administrativo.
- `list_unresolved_links`: lista links Obsidian ainda não resolvidos.
- `resolve_note_link`: associa um link pendente a um caminho de destino.

## Ferramentas Administrativas

As ferramentas administrativas são expostas para operações de curadoria e compatibilidade, não para clientes de agentes.

Documentos e reindexação:

- `get_document`: recupera documento indexado por id ou caminho.
- `list_documents`: lista documentos indexados, opcionalmente por namespace.
- `reindex`: enfileira reindexação de um documento.

Entidades e relações do grafo:

- `get_entity`: recupera uma entidade.
- `search_entities`: busca entidades por nome, aliases, tags e caminhos de origem normalizados.
- `get_related`: lista entidades relacionadas.
- `update_entity`: atualiza propriedades de entidade.
- `merge_entities`: mescla entidades.
- `delete_entity`: remove entidade.

## Contratos Principais

Exemplo de resposta de `search` com um resultado de documento curado:

```json
{
  "results": [
    {
      "id": "doc_001",
      "source": "document",
      "ref": "projetos/brain.md",
      "path": "projetos/brain.md",
      "repo_path": "projetos/brain.md",
      "namespace": "curated",
      "text": "Resumo curado do projeto brain.",
      "score": 0.91
    }
  ],
  "graph": []
}
```

Exemplo de resposta de `deep_search`:

```json
{
  "query": "brain",
  "results": [
    {
      "id": "doc_001",
      "source": "document",
      "ref": "projetos/brain.md",
      "path": "projetos/brain.md",
      "repo_path": "projetos/brain.md",
      "namespace": "curated",
      "text": "Resumo curado do projeto brain.",
      "score": 0.91
    }
  ],
  "graph": {
    "entities": [
      {
        "name": "brain",
        "type": "projeto",
        "namespace": "brain",
        "seed": "brain",
        "depth": 0,
        "matched_by": "substring"
      },
      {
        "name": "Cliente Exemplo",
        "namespace": "brain",
        "type": "cliente",
        "seed": "brain",
        "depth": 1,
        "matched_by": "relationship"
      }
    ],
    "relationships": [
      {
        "from": "Cliente Exemplo",
        "to": "brain",
        "type": "curates",
        "namespace": "brain",
        "seed": "brain",
        "depth": 1
      }
    ]
  },
  "meta": {
    "depth": 1,
    "max_entities": 3,
    "seed_strategy": "substring",
    "namespace_strategy": "single",
    "namespaces": [
      "brain"
    ],
    "rel_types": null,
    "warnings": []
  }
}
```

Exemplo de resposta de `submit_agent_note`:

```json
{
  "note_id": "agent-note-001",
  "repo_path": "_agents/exemplo/2026/06/18/20260618T120000000000-resumo-agent-note-001.md",
  "status": "pending",
  "event_id": "event-001"
}
```

Exemplo abreviado de resposta de `create_agent_client`, com token retornado uma única vez:

O retorno real também inclui os demais campos do perfil do cliente, como `description`, `token_prefix`, `metadata`, `capture_policy`, `recommended_instructions`, `updated_at`, `last_seen_at` e `profile_path`.

```json
{
  "id": "client-001",
  "slug": "exemplo",
  "name": "Cliente Exemplo",
  "status": "active",
  "permissions": [
    "search",
    "deep_search",
    "get_note",
    "submit_agent_note"
  ],
  "token": "brain_client_exemplo_123456",
  "created_at": "2026-06-18T12:00:00+00:00"
}
```

## Compatibilidade E Limites

- `search` permanece uma busca semântica sobre chunks de documentos curados.
- `limit` deve ser um inteiro positivo e é limitado a `50`.
- Valores inválidos de `limit` geram erro.
- `filters.source` aceita `document`, `curated` e `note`; valores não suportados retornam resultados vazios.
- `filters.path_prefix` deve ser relativo, não pode conter `..`, não pode apontar para `_agents/` e não pode conter `%` ou `_`.
- `deep_search.depth` aceita inteiros de 1 a 3.
- `deep_search.max_entities` aceita inteiros de 1 a 3.
- `rel_types` vazio é normalizado para ausência de filtro por tipo de relação.
- `namespace` vazio ou omitido significa consulta global no grafo.
- `namespace` explícito limita apenas a consulta no grafo; a busca semântica continua curada.
- Clientes podem usar `deep_search` globalmente ou com namespace explícito.

## Exemplos De Integração

Requisição MCP HTTP streamable usando token de cliente:

```http
POST /mcp HTTP/1.1
Authorization: Bearer brain_client_exemplo_123456
Content-Type: application/json
```

Exemplo conceitual de chamada de ferramenta:

```json
{
  "tool": "deep_search",
  "arguments": {
    "query": "brain",
    "limit": 5,
    "depth": 1,
    "max_entities": 3,
    "namespace": "brain"
  }
}
```

## Arquivos De Referência

- [`../src/brain/mcp/server.py`](../src/brain/mcp/server.py)
- [`../src/brain/mcp/handlers.py`](../src/brain/mcp/handlers.py)
- [`../src/brain/auth.py`](../src/brain/auth.py)
- [`../src/brain/search/retriever.py`](../src/brain/search/retriever.py)
