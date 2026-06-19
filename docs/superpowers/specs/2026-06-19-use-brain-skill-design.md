# Skill `use-brain` (Design)

- **Data:** 2026-06-19
- **Status:** Design aprovado em brainstorming; aguardando revisao do spec
- **Escopo:** criar uma skill repo-scoped que ensina clientes agentes ja conectados ao MCP do `brain` a buscar contexto e enviar conhecimento duravel para curadoria.

## Objetivo

Criar uma skill versionada neste repositorio para orientar LLMs e agentes de IA
que ja possuem acesso MCP ao `brain`. A skill deve ensinar o cliente comum a
usar o Brain durante o trabalho para:

- buscar contexto, informacoes e conhecimento curado;
- aprofundar consultas quando houver relacoes ou entidades relevantes;
- abrir notas curadas antes de afirmar informacoes importantes;
- enviar conhecimento duravel novo para curadoria.

A skill nao ensina instalacao ou configuracao do MCP. Esse papel pertence a
`.agents/skills/install-brain-mcp/SKILL.md`.

## Fora Do Escopo

- Repetir instrucoes de instalacao do MCP.
- Configurar clientes Codex, Claude, Hermes ou outros clientes MCP.
- Criar, alterar ou remover ferramentas MCP do `brain`.
- Alterar permissoes de clientes.
- Ensinar cliente comum a criar nota curada diretamente.
- Ensinar fluxos de curadoria, como `create_note`, `update_note`,
  `list_agent_notes`, `get_agent_note`, `create_agent_client` ou rotacao de
  tokens.
- Registrar logs amplos de conversa, tarefas transitorias ou dumps sem valor
  futuro claro.

## Local E Formato

A skill sera criada como skill de repositorio em:

```text
.agents/skills/use-brain/SKILL.md
```

O `SKILL.md` sera o unico arquivo da skill. Ele tera frontmatter YAML contendo
apenas `name` e `description`:

```yaml
---
name: use-brain
description: Use Brain as a connected MCP memory for agent clients: retrieve curated context with `search`, `deep_search`, and `get_note`, and submit durable new knowledge for curation with `submit_agent_note`. Use when an agent needs project context, decisions, preferences, reusable knowledge, or guidance on what to preserve in Brain.
---
```

A descricao deve acionar a skill quando o usuario ou agente precisar usar o
Brain como memoria MCP ja conectada: buscar contexto, recuperar notas curadas,
aprofundar conhecimento com grafo/contexto relacionado ou enviar conhecimento
duravel novo por `submit_agent_note`.

O corpo da skill sera escrito em ingles para ser consumido diretamente por
agentes/LLMs, seguindo o estilo da skill existente `install-brain-mcp`.

## Modelo Mental

A skill deve ensinar que o Brain e uma memoria curada.

Para clientes comuns:

- leitura acontece sobre conteudo curado;
- `search`, `deep_search` e `get_note` sao ferramentas de consulta;
- escrita acontece por `submit_agent_note`, que cria uma nota bruta para
  curadoria posterior;
- `submit_agent_note` nao garante que uma nota curada ja exista;
- `_agents/` e inbox bruta e nao deve ser tratada como superficie publica de
  leitura;
- ferramentas administrativas e de curadoria estao fora do papel do cliente.

## Arquitetura Da Skill

A skill sera um workflow unico: "usar Brain durante o trabalho".

O `SKILL.md` tera estas secoes:

- **Core Mental Model:** Brain como memoria curada; cliente consulta curado e
  contribui conhecimento bruto para curadoria.
- **Start With Retrieval:** quando buscar no Brain antes de responder ou agir.
- **Retrieval Workflow:** quando usar `search`, `deep_search` e `get_note`.
- **Contribution Workflow:** quando usar `submit_agent_note`.
- **Durable Knowledge Criteria:** criterios para decidir se algo merece virar
  memoria.
- **Note Quality Rules:** como escrever uma submissao boa para curadoria.
- **What Not To Store:** segredos, dados sensiveis desnecessarios, logs
  transitorios, dumps grandes e ruido.
- **Error Handling:** resultados vazios, `get_note` nulo, falta de permissao
  para submissao e incerteza sobre curadoria.
- **Output Expectations:** como comunicar ao usuario que usou contexto curado ou
  enviou conhecimento para curadoria.

## Ferramentas De Cliente Cobertas

### `search`

Usar para busca direta sobre conhecimento curado.

Casos principais:

- pergunta direta sobre um topico, pessoa, projeto, decisao ou preferencia;
- procurar notas por termos provaveis;
- encontrar resultados candidatos antes de abrir notas completas.

A skill deve orientar o agente a usar `limit` moderado e a tratar resultados
como candidatos, nao como prova final quando a afirmacao for importante.

### `deep_search`

Usar quando a tarefa exigir contexto amplo, relacoes ou entidades conectadas.

Casos principais:

- entender historico de projeto;
- recuperar contexto relacionado a uma entidade;
- investigar relacoes entre pessoas, projetos, decisoes, sistemas ou conceitos;
- buscar quando uma consulta direta pode perder conexoes relevantes.

A skill deve explicar que `depth`, `max_entities`, `rel_types` e `namespace`
devem ser usados com parcimonia. Valores amplos demais podem produzir ruido.

### `get_note`

Usar para abrir notas curadas relevantes retornadas por `search` ou
`deep_search`.

Casos principais:

- confirmar fonte antes de responder;
- recuperar conteudo completo quando o trecho do resultado for insuficiente;
- ler contexto, metadados e conteudo integral de uma nota curada.

A skill deve instruir o agente a aceitar `null` como "nota nao encontrada" e a
nao tentar abrir `_agents/` como cliente.

### `submit_agent_note`

Usar para enviar conhecimento duravel novo para curadoria.

Casos principais:

- fato novo e reutilizavel;
- decisao tomada;
- preferencia estavel do usuario;
- aprendizado sobre projeto, sistema ou processo;
- descoberta que sera util em sessoes futuras;
- contexto de trabalho que deve ser preservado.

A skill deve orientar o agente a enviar notas autocontidas, com titulo claro,
conteudo suficiente, origem/contexto quando util, `suggested_namespace` quando
conhecido e metadata curta quando ajudar curadoria.

## Fluxo De Recuperacao

A skill deve ensinar este ciclo:

1. Antes de agir, avaliar se a tarefa depende de contexto acumulado.
2. Usar `search` para perguntas diretas.
3. Usar `deep_search` para contexto amplo ou relacionado.
4. Usar `get_note` para abrir as notas mais relevantes antes de afirmar algo
   importante.
5. Responder com base no contexto curado quando houver suporte.
6. Declarar incerteza quando a busca nao encontrar apoio suficiente.

## Fluxo De Contribuicao

A skill deve ensinar este ciclo:

1. Identificar conhecimento duravel novo durante o trabalho.
2. Confirmar que nao e segredo, dado sensivel desnecessario, log transitorio ou
   ruido.
3. Escrever uma nota bruta autocontida para curadoria.
4. Usar `submit_agent_note` com `title`, `content` ou `messages`,
   `suggested_namespace` e `metadata` quando apropriado.
5. Informar que o conhecimento foi enviado para curadoria, nao que ja virou nota
   curada.

## Criterios De Conhecimento Duravel

A skill deve priorizar informacoes que provavelmente serao uteis em sessoes
futuras:

- fatos estaveis;
- decisoes e justificativas;
- preferencias do usuario;
- aprendizados reutilizaveis;
- contexto de projeto;
- descobertas tecnicas ou operacionais;
- mapeamentos de nomes, sistemas, repositorios, processos ou entidades.

A skill deve rejeitar como memoria:

- segredos, tokens, credenciais ou dados privados desnecessarios;
- transcricoes longas sem sintese;
- logs de execucao efemeros;
- tarefas pequenas sem valor futuro;
- conteudo incerto sem rotulo de incerteza;
- material que o usuario pediu para nao persistir.

## Tratamento De Erros

A skill deve diagnosticar:

- resultados vazios em `search`: reformular consulta, usar sinonimos ou tentar
  `deep_search`;
- resultados vazios em `deep_search`: reduzir filtros, remover namespace ou
  voltar para `search`;
- `get_note` retorna `null`: tratar como nota nao encontrada e nao inventar
  conteudo;
- `submit_agent_note` sem permissao: informar que o cliente nao pode submeter
  notas e que um curador precisa ajustar permissoes;
- erro por conteudo ausente em `submit_agent_note`: enviar `content` ou
  `messages`;
- tentativa de usar ferramenta de curador: recusar o caminho e manter o fluxo de
  cliente comum.

## Validacao

Depois da implementacao, validar:

- `SKILL.md` existe em `.agents/skills/use-brain/`.
- Frontmatter contem `name: use-brain` e `description`.
- O nome usa apenas letras minusculas e hifens.
- A descricao menciona uso do Brain como memoria MCP ja conectada.
- A skill cobre `search`, `deep_search`, `get_note` e `submit_agent_note`.
- A skill nao orienta cliente comum a usar ferramentas de curador.
- A skill explica a diferenca entre nota curada e nota bruta enviada para
  curadoria.
- A skill orienta criar notas apenas para conhecimento duravel.
- A skill nao contem tokens reais, segredos, marcadores de scaffold,
  reticencias ou placeholders ambiguos.
- A skill e consistente com `docs/mcp-api.md`, `docs/security.md` e
  `.agents/skills/install-brain-mcp/SKILL.md`.

## Referencias

- `docs/mcp-api.md`
- `docs/security.md`
- `.agents/skills/install-brain-mcp/SKILL.md`
- `src/brain/mcp/handlers.py`
- `src/brain/mcp/server.py`
- `tests/integration/test_mcp_handlers.py`
