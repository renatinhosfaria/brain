# brain — Entidades Semânticas De Notas Curadas (Design)

- **Data:** 2026-06-20
- **Status:** Design aprovado em brainstorming; aguardando revisão do spec
- **Escopo:** PR 1 para entidade determinística por nota curada, aliases e busca de entidades melhorada. Reindexação recursiva por pasta ou namespace fica fora da primeira entrega.

## Objetivo

Corrigir a lacuna em que `search` encontra notas curadas, mas `search_entities` não encontra entidades correspondentes por título, aliases, tags, path ou variações normalizadas.

A primeira entrega deve gerar uma entidade determinística para cada nota curada individual elegível, sincronizá-la durante criação, atualização e reindexação individual de arquivo Markdown, e ampliar `search_entities` para consultar nome, aliases, tags e path normalizados.

## Fora Do Escopo

- Implementar reindexação recursiva de pasta.
- Implementar reindexação global de namespace.
- Transformar aliases em geração combinatória ampla.
- Criar entidades canônicas a partir de `_agents/`.
- Depender de LLM para a entidade principal derivada da nota.
- Alterar a busca documental `search`.
- Alterar a superfície pública de retorno de `search_entities`, salvo campos adicionais compatíveis se forem úteis.

## Arquitetura

`pipeline.index_document` continua sendo o ponto único de orquestração para documentos indexados. Depois de persistir ou atualizar o `Document`, a pipeline chama uma função encapsulada no módulo novo `brain.ingestion.semantic_entities`.

Fluxo de alto nível:

```text
pipeline.index_document(...)
  -> upsert Document
  -> semantic_entities.upsert_entity_from_curated_document(...)
  -> chunks/embeddings
  -> entidades/relações LLM existentes
  -> commit pelo chamador atual
```

A pipeline apenas orquestra. O conhecimento específico de que uma nota curada gera uma entidade determinística fica encapsulado no módulo novo.

A chamada acontece somente para documentos elegíveis:

- `namespace == "curated"`;
- `repo_path` Markdown;
- `repo_path` fora de `_agents/`.

A função também valida essa elegibilidade internamente para ficar segura quando for reutilizada por futuros fluxos como `reindex_document`, `reindex_directory` e `reindex_namespace`.

## Escopo Do PR 1 E Extensão Futura

Implementar agora:

- entidade determinística derivada de nota curada individual;
- aliases derivados de `metadata.title`, H1, path, slug, tags e aliases explícitos;
- `search_entities` pesquisando em `name`, aliases, tags e path/source_doc normalizados;
- upsert real de entidade;
- integração com `create_note`, `update_note` e reindexação individual quando `repo_path` aponta para arquivo `.md` ou `.markdown`;
- testes automatizados.

Preparar o design interno para depois:

```text
reindex_document(repo_path)
reindex_directory(prefix)
reindex_namespace(namespace)
```

Nesta entrega, apenas o equivalente a `reindex_document(repo_path)` precisa funcionar pelo caminho existente de `pipeline.index_document` ou job individual de arquivo.

## Componentes E Contratos

### `brain.ingestion.semantic_entities`

Responsabilidade: concentrar a regra determinística de entidades derivadas de notas curadas.

Assinatura interna proposta:

```python
upsert_entity_from_curated_document(
    session,
    *,
    namespace: str,
    repo_path: str,
    title: str | None,
    content: str,
    metadata: dict | None,
    document_id: str | None = None,
) -> dict
```

`session` é a sessão transacional já usada pela pipeline e pelo document store. A função não faz `commit` próprio.

A função valida internamente que o documento é elegível:

- pertence ao namespace `curated`;
- é Markdown, identificado inicialmente por sufixo `.md` ou `.markdown`;
- não está em `_agents/` nem abaixo de `_agents/`;
- possui nome canônico derivável com segurança.

O retorno deve ser sempre um resumo da entidade criada, atualizada ou ignorada. Documentos inelegíveis ou sem nome útil retornam `status = "skipped"` em vez de `None`:

```python
{
    "status": "created" | "updated" | "skipped",
    "name": str | None,
    "namespace": str,
    "type": str | None,
    "source_doc": str,
    "aliases": list[str],
    "reason": str | None,
}
```

O módulo é responsável por:

- escolher nome canônico;
- mapear tipo;
- gerar aliases;
- normalizar termos;
- montar props;
- chamar a camada AGE.

### Nome Canônico

A entidade determinística escolhe o nome canônico nesta ordem:

1. `metadata.title`, se existir e não estiver vazio.
2. Primeiro heading Markdown H1 (`# Titulo`), se existir.
3. Nome humanizado derivado do filename/path.
4. Skip seguro, com log `warning` ou `debug`, se nenhum nome útil puder ser derivado.

Mesmo quando `metadata.title` ou H1 forem usados como nome canônico, o slug/path deve entrar nos aliases.

Não usar path técnico como nome canônico se houver H1 humano disponível.

### Tipo

O tipo vem de `metadata.type` quando existir, com normalização conhecida:

| `metadata.type` | Tipo no grafo |
| --- | --- |
| `project` | `projeto` |
| `preference` | `preferencia` |
| `decision` | `decisao` |
| `process` | `processo` |
| `concept` | `conceito` |
| `reference` | `referencia` |
| `map` | `mapa` |

Se `metadata.type` estiver ausente ou for desconhecido, usar `conceito`. Quando houver tipo desconhecido, preservar o valor original em `props.raw_type` para diagnóstico.

### Props

Props mínimas da entidade determinística:

- `source_doc`;
- `repo_path`;
- `document_id`, quando disponível;
- `title`;
- `status`, quando disponível;
- `tags`;
- `aliases`;
- campos normalizados, quando úteis para busca;
- `raw_type`, quando `metadata.type` desconhecido existir;
- `updated_at`, quando disponível.

`source_doc` e `repo_path` devem permitir reconciliação futura por documento.

### Camada AGE

A camada `brain.graph.age` deve ganhar helpers pequenos, sem espalhar Cypher pelo módulo novo:

```python
find_entity_by_source_doc(namespace, repo_path, document_id=None)
upsert_entity(...)
```

Se a implementação preferir um helper dedicado para atualizar identidade por origem documental, ele deve preservar relações existentes:

```python
update_entity_identity_by_source_doc(...)
```

Regra de escrita:

1. tentar localizar entidade determinística existente por `source_doc`, `repo_path` ou `document_id`;
2. se existir, atualizar o próprio nó com props, aliases, tipo e `name`, preservando relações existentes;
3. se não existir, cair para o upsert atual por `(name, namespace)`.

A primeira versão segue a identidade atual do grafo AGE, normalmente `(name, namespace)`, mas persiste `source_doc`, `repo_path` e `document_id` para detectar, atualizar ou limpar por documento no futuro.

Mudança de título não deve criar duplicata silenciosa para o mesmo `source_doc`. No PR 1, uma entidade encontrada por origem documental deve ser atualizada em vez de criar uma segunda entidade determinística para o mesmo documento.

Os helpers AGE usados por essa função devem operar na mesma sessão/transação recebida e não devem fazer `commit` próprio.

## Fluxo E Tratamento De Erros

Em `pipeline.index_document`, a ordem será:

1. upsert do `Document`;
2. sincronização da entidade determinística, se o documento curado for elegível;
3. chunks/embeddings;
4. entidades/relações LLM existentes;
5. commit pelo chamador atual.

A sincronização da entidade determinística deve rodar também quando o `content_hash` for igual. Nesse caso, a pipeline pode pular recomputação de chunks, embeddings e extração LLM, mas não deve retornar antes de atualizar os metadados do `Document` e executar `upsert_entity_from_curated_document`.

`content_hash` igual não pode retornar antes da sincronização da entidade determinística.

Isso cobre reindexação individual de notas antigas e mudanças apenas em `metadata.title`, `metadata.type` ou `tags`, sem exigir alteração do corpo Markdown.

Se o documento não for elegível, a função retorna `status = "skipped"`, sem erro.

Falha real na sincronização da entidade determinística deve falhar a indexação do documento. A falha não deve ser silenciada, para evitar estado em que o documento parece indexado, mas a entidade determinística não foi sincronizada.

Se AGE não puder participar da mesma transação no PR 1, falhas ainda devem ser propagadas e logadas. A reconciliação posterior por reindexação individual fica como mecanismo de reparo.

## Aliases

A geração de aliases será determinística e conservadora.

Entradas:

- nome canônico;
- H1;
- `repo_path`;
- filename/slug humanizado;
- `metadata.tags` ou `tags`;
- campos explícitos de alias no metadata, como `aliases`.

Para cada entrada, o módulo gera versões normalizadas com:

- `casefold`;
- remoção de acentos;
- remoção de pontuação excessiva;
- hífens substituídos por espaços;
- preservação controlada de termos técnicos relevantes, como `.env`;
- partes separadas por vírgula, barra e conectores simples;
- combinações curtas explicitamente úteis e cobertas por testes.

A geração não deve produzir todas as combinações possíveis de tokens. Aliases genéricos demais devem ser descartados, salvo whitelist técnica ou de domínio.

Exemplos de aliases genéricos a evitar quando isolados: `projeto`, `regras`, `perfil`, `tecnica`, `deve`.

Exemplos de termos curtos úteis permitidos por whitelist técnica/domínio: `.env`, `env`, `CEO`, `migrations`.

Aliases de domínio como `Hermes CEO` e `ceo hermes` só devem ser gerados quando vierem de `metadata.aliases` ou de regra de domínio explicitamente codificada e testada para esse caso.

Exemplos obrigatórios:

Para `Privacidade, credenciais e ações externas`:

- `privacidade`;
- `credenciais`;
- `ações externas`;
- `acoes externas`;
- `privacidade credenciais`;
- `privacidade credenciais acoes externas`.

Para `Stack técnica deve ser inferida por projeto`:

- `stack técnica`;
- `stack tecnica`;
- `stack por projeto`;
- `stack técnica por projeto`;
- `stack tecnica por projeto`.

Para `Regras de .env e migrations dependem do projeto`:

- `.env`;
- `env`;
- `migrations`;
- `env migrations`;
- `regras env`;
- `migrations por projeto`;
- `regras de env e migrations`.

Para `Perfil CEO`, quando houver alias explícito ou regra de domínio testada:

- `CEO`;
- `perfil ceo`;
- `Hermes CEO`;
- `ceo hermes`.

## Busca E Ranking

`search_entities(query, namespace)` deve pesquisar:

- `name`;
- `props.aliases`;
- `props.tags`;
- `props.source_doc`;
- `props.repo_path`;
- versões normalizadas sem acento e case-insensitive.

Pode normalizar em tempo de busca ou persistir campos como `props.name_normalized`, `props.aliases_normalized` e `props.tags_normalized`. Para PR 1, a escolha deve favorecer simplicidade e testes claros.

O retorno público pode permanecer compatível:

```json
{
  "name": "Perfil CEO",
  "type": "conceito",
  "namespace": "curated"
}
```

Ranking simples:

1. nome exato normalizado;
2. alias exato normalizado;
3. tag exata normalizada;
4. prefixo em nome/alias;
5. contains em nome/alias;
6. contains em tags/path/source_doc.

Path e `source_doc` rankeiam por último para evitar que termos comuns no caminho vençam nomes ou aliases semanticamente melhores.

## Reindexação Individual

No PR 1, "reindexação individual" significa o caminho já existente de arquivo único:

- `pipeline.index_document` chamado diretamente para documento existente;
- job de reindexação quando `repo_path` aponta para arquivo `.md` ou `.markdown`.

O design não exige suporte a prefixo de pasta nem namespace inteiro nesta entrega.

## Testes E Critérios De Aceitação

### Camada Pura: `brain.ingestion.semantic_entities`

Testar sem Postgres/AGE:

- `metadata.title` vence H1;
- H1 vence path humanizado;
- path humanizado é usado quando não há title nem H1;
- documento sem nome útil retorna `status = "skipped"`;
- path/slug entra como alias mesmo quando não é nome canônico;
- tipos conhecidos são normalizados para português;
- tipo ausente ou desconhecido cai para `conceito`;
- `props.raw_type` é preservado quando `metadata.type` desconhecido existir;
- normalização por casefold e remoção de acentos;
- hífen como espaço;
- descarte de aliases genéricos;
- preservação de termos técnicos/domínio via whitelist;
- geração sem combinação explosiva;
- exemplos obrigatórios de aliases.

### Camada AGE/Grafo

Testar com entidades contendo:

- `props.aliases`;
- `props.tags`;
- `props.source_doc`;
- `props.repo_path`;
- campos normalizados, quando persistidos.

Casos de busca/ranking:

- match por nome exato vence alias;
- alias exato vence tag;
- tag vence prefixo/contains;
- path/source_doc rankeia por último.

Casos de escrita:

- `upsert_entity` de entidade inexistente cria entidade recuperável;
- depois do upsert, `get_entity` ou `search_entities` recupera a entidade;
- atualização de entidade existente preserva ou mescla aliases e props esperadas.

### Camada De Integração

Testar:

- `create_note`;
- `update_note`;
- `pipeline.index_document` chamado diretamente como reindexação individual de arquivo existente;
- nota em namespace `curated`;
- nota fora de `_agents/`;
- nota em `_agents/` não cria entidade determinística;
- `content_hash` igual não retorna antes da sincronização semântica;
- alteração apenas em `metadata.title`, `metadata.type` ou `tags` atualiza entidade/aliases sem exigir mudança no corpo Markdown;
- mudança de título com entidade existente por `source_doc` não cria duplicata silenciosa.

Usar fixtures sintéticas para regressões, sem depender do vault real.

Casos obrigatórios de consulta:

- `search_entities("Stack técnica por projeto", "curated")` retorna a entidade derivada de `preferencias/stack-tecnica-por-projeto.md`;
- `search_entities("stack tecnica", "curated")` retorna a mesma entidade;
- `search_entities("env migrations", "curated")` retorna a entidade derivada de `preferencias/regras-env-e-migrations-por-projeto.md`;
- `search_entities("migrations por projeto", "curated")` retorna a mesma entidade;
- `search_entities("Privacidade", "curated")` retorna a entidade derivada de `preferencias/privacidade-credenciais-e-acoes-externas.md`;
- `search_entities("credenciais", "curated")` retorna a mesma entidade;
- `search_entities("Hermes CEO", "curated")` retorna `Perfil CEO` quando alias explícito ou regra de domínio estiver presente;
- fixtures sintéticas representando `FamaAgent`, `mcp-fama`, `Evolution-go` e `Paperclip` continuam recuperáveis.

Critério de aceitação:

- notas curadas novas criam ou atualizam entidade determinística;
- notas curadas antigas passam a ser recuperáveis ao reindexar individualmente;
- `search_entities` encontra por nome, alias, tag e path normalizados;
- ranking prioriza nome/alias/tag antes de path;
- `_agents/` permanece excluído;
- falhas reais de AGE não são silenciadas.

## Arquivos Prováveis

- `src/brain/ingestion/pipeline.py`
- `src/brain/ingestion/semantic_entities.py`
- `src/brain/graph/age.py`
- `src/brain/mcp/handlers.py`
- `tests/test_semantic_entities.py`
- `tests/integration/test_graph.py`
- `tests/integration/test_pipeline.py`
- `tests/integration/test_mcp_handlers.py`
