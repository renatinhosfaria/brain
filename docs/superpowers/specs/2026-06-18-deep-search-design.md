# brain — Deep Search com Knowledge Graph (Design)

- **Data:** 2026-06-18
- **Status:** Design aprovado em brainstorming; aguardando revisão do spec
- **Escopo:** criar uma nova ferramenta MCP de busca profunda, sem alterar o `search` atual.

## Objetivo

Adicionar uma ferramenta MCP separada, `deep_search`, que combine evidência textual de notas curadas com contexto relacional vindo do Knowledge Graph em Apache AGE.

A ferramenta resolve perguntas que exigem seguir relações entre entidades. A busca rápida atual continua sendo o caminho padrão para perguntas pontuais baseadas em chunks semânticos.

## Fora do Escopo

- Alterar o comportamento do `search` existente.
- Incluir `memories` na busca pública.
- Buscar em `_agents/`.
- Gerar resumo ou reranking com LLM dentro do servidor.
- Transformar toda consulta em Graph RAG.

## Contrato MCP

```text
deep_search(
  query: str,
  limit: int = 10,
  depth: int = 1,
  max_entities: int = 3,
  rel_types: list[str] | None = None,
  filters: dict | None = None,
  namespace: str = "curated"
)
```

Regras:

- `search` permanece inalterado.
- `deep_search` sempre tenta retornar texto e grafo.
- `depth` tem padrão `1` e limite rígido `3`.
- `max_entities` tem padrão `3` e limite rígido `3`.
- `rel_types` é opcional; quando ausente ou vazio, todos os tipos de relação são aceitos.
- `limit` controla apenas a quantidade de resultados textuais.
- O grafo usa limite interno de 50 relações para evitar payloads grandes.
- O texto é buscado apenas em chunks de notas curadas.
- O parâmetro `namespace` controla a busca de entidades e relações no grafo; ele não libera busca textual fora de notas curadas.

## Payload

```json
{
  "query": "Como Hermes se relaciona com o brain?",
  "results": [
    {
      "id": "document-id",
      "source": "document",
      "ref": "projetos/brain.md",
      "path": "projetos/brain.md",
      "repo_path": "projetos/brain.md",
      "namespace": "curated",
      "text": "chunk relevante...",
      "score": 0.87
    }
  ],
  "graph": {
    "entities": [
      {
        "name": "brain",
        "type": "projeto",
        "seed": "brain",
        "depth": 0,
        "matched_by": "substring"
      },
      {
        "name": "Hermes",
        "type": "agente",
        "seed": "brain",
        "depth": 1,
        "matched_by": "relationship"
      }
    ],
    "relationships": [
      {
        "from": "Hermes",
        "to": "brain",
        "type": "curates",
        "seed": "brain",
        "depth": 1
      }
    ]
  },
  "meta": {
    "depth": 1,
    "max_entities": 3,
    "seed_strategy": "substring",
    "rel_types": null,
    "warnings": []
  }
}
```

O servidor retorna dados estruturados. A síntese final fica a cargo do agente que chamou a ferramenta.

## Componentes

### `brain.graph.age.get_relationship_paths`

Responsabilidade: consultar o Apache AGE e devolver entidades e relações estruturadas.

Assinatura interna:

```text
get_relationship_paths(
  session,
  seeds: list[str],
  namespace: str,
  depth: int = 1,
  rel_types: list[str] | None = None,
  limit: int = 50
) -> dict
```

Comportamento:

- Executa uma travessia de caminhos de comprimento variável a partir das sementes.
- Usa o grafo `brain` e entidades `Entity` filtradas por `name` e `namespace`.
- Retorna entidades com `name`, `type`, `seed`, `depth`.
- Retorna relações com `from`, `to`, `type`, `seed`, `depth`.
- Deduplica no Python:
  - entidade por `(name, namespace)`;
  - relação por `(from, to, type, seed, depth)`.
- Aplica `rel_types` no Python.
- Interrompe a montagem do resultado ao atingir o limite interno.

A consulta deve usar caminhos variáveis do Apache AGE, por exemplo:

```cypher
MATCH p = (s:Entity {name: "...", namespace: "..."})-[*1..depth]-(n:Entity)
RETURN nodes(p), relationships(p)
```

A implementação deve preservar a direção real da aresta usando os campos nativos de aresta do AGE, como `start_id` e `end_id`, quando eles estiverem disponíveis no retorno. Se os testes mostrarem que esse parsing não é estável na versão atual do AGE, a implementação deve usar consultas direcionadas equivalentes para preservar `from` e `to`.

### `brain.extraction.query_entities`

Responsabilidade: fallback de LLM para extrair nomes de entidades da pergunta.

Comportamento:

- Recebe a query do usuário.
- Retorna no máximo `max_entities` nomes candidatos.
- Usa prompt estrito para extrair apenas entidades-chave.
- Não substitui a busca textual; só roda quando o fast path não encontra sementes.

### `brain.search.retriever.deep_search`

Responsabilidade: orquestrar a recuperação profunda.

Fluxo:

1. Gera embedding da query.
2. Busca chunks curados com a mesma base do `search` atual.
3. Resolve entidades-semente:
   - primeiro `age.search_entities(query, namespace)`;
   - se não houver resultado, usa `query_entities` como fallback;
   - resolve cada nome retornado pelo LLM com `age.search_entities(nome, namespace)`;
   - deduplica entidades resolvidas;
   - limita a `max_entities`.
4. Se houver sementes, chama `age.get_relationship_paths`.
5. Retorna `results`, `graph` e `meta`.

### `brain.mcp.handlers.deep_search` e `brain.mcp.server`

Responsabilidade: expor a ferramenta MCP e defender o contrato público.

Validações:

- exige principal autenticado como cliente ou curador;
- normaliza `limit` com o limite existente;
- rejeita `depth < 1` ou `depth > 3`;
- rejeita `max_entities < 1` ou `max_entities > 3`;
- trata `rel_types=[]` como `None`;
- repassa `filters` apenas quando for `dict`.

## Tratamento de Erros

- Sem entidades-semente: retorna chunks normalmente, grafo vazio e `meta.seed_strategy = "none"`.
- Fallback LLM falha: retorna chunks normalmente, grafo vazio e aviso em `meta.warnings`.
- Consulta AGE falha: retorna erro da ferramenta. `deep_search` foi chamado para obter grafo, então uma falha estrutural do grafo não deve ser mascarada como busca bem-sucedida.
- `depth` ou `max_entities` fora dos limites: erro claro no handler.
- `rel_types` vazio: interpretado como ausência de filtro.

## Testes

### Grafo

Adicionar testes de integração em `tests/integration/test_graph.py`:

- retorna entidades e relações a partir de uma seed;
- preserva direção da relação;
- calcula profundidade;
- deduplica entidades e relações;
- aplica `rel_types`;
- respeita limite interno de relações.

### Retriever

Adicionar testes em `tests/integration/test_retriever.py`:

- combina chunks curados e grafo;
- usa fast path por `search_entities`;
- usa fallback LLM quando substring não encontra seeds;
- retorna grafo vazio com `seed_strategy = "none"` quando não há seeds;
- retorna chunks e warning quando fallback LLM falha;
- não inclui `memories` nem `_agents/`.

### MCP

Adicionar testes em `tests/integration/test_mcp_handlers.py`:

- expõe `deep_search`;
- mantém `search` inalterado;
- rejeita `depth` inválido;
- rejeita `max_entities` inválido;
- aceita `rel_types` opcional;
- exige autenticação compatível com `search`.

## Compatibilidade

Esta entrega é aditiva. Clientes existentes continuam usando `search` sem alteração. Agentes que precisam seguir relações usam `deep_search` explicitamente.

## Fontes Técnicas

- Apache AGE `MATCH` e caminhos de comprimento variável: https://age.apache.org/age-manual/master/clauses/match.html
- Apache AGE `agtype`, `vertex`, `edge` e `path`: https://age.apache.org/age-manual/master/intro/types.html
