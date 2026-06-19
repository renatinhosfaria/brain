# brain — Documentação Técnica Do Projeto (Design)

- **Data:** 2026-06-18
- **Status:** Design aprovado em brainstorming; aguardando revisão do spec
- **Escopo:** criar uma estrutura de documentação técnica em Markdown para mantenedores e integradores MCP.

## Objetivo

Criar uma documentação técnica moderna, versionada e navegável para o projeto
`brain`, escrita em português técnico e organizada em múltiplos arquivos dentro de
`docs/`.

A documentação deve atender dois públicos principais:

- **Mantenedores do projeto:** pessoas que precisam entender arquitetura, módulos,
  fluxos internos, dados, testes, deploy e decisões técnicas.
- **Integradores MCP:** pessoas ou agentes que precisam consumir o servidor MCP,
  entender autenticação, ferramentas disponíveis, contratos, limites e permissões.

O README raiz continua sendo guia rápido de produção. A nova documentação em
`docs/` passa a ser a referência técnica detalhada.

## Fora Do Escopo

- Alterar código de produção.
- Reestruturar módulos Python.
- Criar site estático de documentação.
- Gerar documentação automatizada a partir de schemas.
- Expor segredos, valores reais de `.env` ou tokens.
- Documentar comportamento desejado que o código atual ainda não implementa.
- Reescrever o README raiz, exceto por um possível link curto para `docs/`.

## Princípios De Organização

A documentação será organizada por necessidade de leitura, inspirada em Diátaxis:

- **Explicação:** arquitetura, decisões, fronteiras e racional técnico.
- **Referência:** contratos MCP, modelo de dados, variáveis e permissões.
- **How-to:** desenvolvimento local, testes, deploy, backup e troubleshooting.

Também serão usados:

- **C4 simplificado:** diagramas de contexto, containers e componentes.
- **Mermaid:** diagramas versionáveis dentro dos arquivos `.md`.
- **ADRs:** registros curtos para decisões arquiteturais relevantes.

Referências externas:

- Diátaxis: https://diataxis.fr/
- C4 Model: https://c4model.com/
- Documenting Architecture Decisions: https://www.cognitect.com/blog/2011/11/15/documenting-architecture-decisions
- Architecture Decision Record, Martin Fowler: https://martinfowler.com/bliki/ArchitectureDecisionRecord.html

## Estrutura Proposta

```text
docs/
  README.md
  architecture.md
  mcp-api.md
  development.md
  operations.md
  data-model.md
  security.md
  decisions/
    README.md
    0001-documentation-architecture.md
```

### `docs/README.md`

Porta de entrada da documentação técnica.

Conteúdo esperado:

- Visão geral do `brain`.
- Mapa de navegação por perfil de leitor.
- Links para arquitetura, MCP, desenvolvimento, operação, dados, segurança e ADRs.
- Convenções da documentação: idioma, Mermaid, links relativos e atualização junto
  com mudanças relevantes.

### `docs/architecture.md`

Explicação da arquitetura atual.

Conteúdo esperado:

- Propósito do `brain`: servidor MCP e API operacional para vault Markdown curado.
- Atores externos: clientes MCP, Hermes curador, operadores, GitHub/vault e OpenAI.
- Containers: `api`, `worker`, `postgres`, `repo_cache`, Caddy e backup.
- Componentes internos:
  - `brain.main`
  - `brain.mcp.server`
  - `brain.mcp.handlers`
  - `brain.storage.repositories`
  - `brain.ingestion.pipeline`
  - `brain.search.retriever`
  - `brain.graph.age`
  - `brain.queue`
  - `brain.outbox`
  - `brain.auth`
- Fluxos principais com Mermaid:
  - webhook GitHub para indexação;
  - criação ou atualização de nota curada;
  - submissão de nota bruta por cliente;
  - busca semântica;
  - `deep_search`;
  - entrega de evento do outbox para Hermes.
- Fronteiras arquiteturais:
  - notas curadas entram em busca e leitura MCP;
  - `_agents/` é inbox bruto e não entra na busca pública;
  - `memories` não entram na busca pública MCP;
  - ferramentas administrativas de grafo são restritas ao curador.

### `docs/mcp-api.md`

Referência para integradores MCP e mantenedores de handlers.

Conteúdo esperado:

- Endpoint `/mcp` com transporte streamable HTTP.
- Autenticação por `Authorization: Bearer`.
- Principais tipos de principal:
  - `curator`;
  - `client`;
  - acesso operacional separado via `BRAIN_AUTH_TOKEN` apenas para `/status`.
- Ferramentas de cliente:
  - `search`;
  - `deep_search`;
  - `get_note`;
  - `submit_agent_note`.
- Ferramentas de curadoria:
  - gerenciamento de clientes;
  - ciclo de vida de notas brutas;
  - criação e atualização de notas curadas;
  - árvore do vault;
  - links;
  - ferramentas administrativas de documentos e grafo.
- Contratos de entrada e saída das ferramentas principais com exemplos JSON
  curtos.
- Regras de compatibilidade:
  - preservar comportamento público de `search`;
  - normalizar `limit`, `namespace` e `rel_types`;
  - respeitar limites de `depth` e `max_entities` em `deep_search`;
  - manter leitura global de grafo via `deep_search` para cliente e curador.
- Invariantes de permissão:
  - clientes não leem `_agents/`;
  - clientes não gerenciam outros clientes;
  - clientes não acessam ferramentas administrativas de grafo;
  - escrita de cliente continua restrita ao inbox próprio.

### `docs/development.md`

Guia para mantenedores.

Conteúdo esperado:

- Requisitos locais: Python 3.12, `uv`, Docker e Postgres com pgvector/AGE.
- Setup de desenvolvimento.
- Como rodar testes unitários e de integração.
- Estrutura dos pacotes em `src/brain`.
- Fluxo para adicionar:
  - ferramenta MCP;
  - migration Alembic;
  - job de worker;
  - fluxo de ingestão;
  - índice ou consulta de busca;
  - mudança de grafo AGE.
- Convenções práticas:
  - manter contratos nos handlers;
  - proteger path traversal com `normalize_repo_path`;
  - não indexar `_agents/`;
  - separar orquestração, persistência e exposição MCP;
  - escrever testes de integração quando tocar Postgres, fila, worker, MCP ou AGE.

### `docs/operations.md`

Guia operacional para produção.

Conteúdo esperado:

- Docker Compose e serviços.
- Profiles `proxy` e `backup`.
- Variáveis de ambiente por categoria.
- Startup, migrations e healthchecks.
- Endpoints `/health`, `/status`, `/webhook/github` e `/mcp`.
- Worker, fila, backoff e jobs travados.
- Outbox para Hermes e assinaturas HMAC.
- Backup e restauração.
- Troubleshooting:
  - banco indisponível;
  - webhook GitHub rejeitado;
  - worker sem processar;
  - falha de push Git;
  - falha de embedding ou LLM;
  - AGE/pgvector ausentes.

### `docs/data-model.md`

Referência do modelo persistente.

Conteúdo esperado:

- Tabelas SQLAlchemy:
  - `namespaces`;
  - `documents`;
  - `chunks`;
  - `memories`;
  - `ingestion_jobs`;
  - `agent_clients`;
  - `agent_notes`;
  - `outbox_events`;
  - `note_links`.
- Relação entre fonte de verdade e índices derivados:
  - vault Markdown e Postgres;
  - chunks e embeddings;
  - grafo AGE;
  - links extraídos;
  - outbox e fila operacional.
- Mermaid ER simplificado.
- Regras de reconstrução:
  - documentos e chunks podem ser reindexados a partir do vault;
  - grafo derivado de extração pode ser refeito por reindexação;
  - eventos do outbox e notas brutas são registros operacionais e não devem ser
    descartados sem decisão explícita.

### `docs/security.md`

Referência de segurança e fronteiras de confiança.

Conteúdo esperado:

- Modelo de autenticação:
  - token de curador;
  - token de cliente;
  - token operacional de `/status`;
  - HMAC de webhook GitHub;
  - HMAC de outbox para Hermes.
- Armazenamento de tokens:
  - hash para autenticação;
  - token recuperável criptografado com Fernet;
  - prefixo seguro para identificação.
- Permissões por principal.
- Riscos conhecidos:
  - `_agents/` não é barreira criptográfica;
  - notas brutas podem conter dados sensíveis;
  - vault remoto deve permanecer privado;
  - `REPO_URL`, tokens e chaves nunca devem entrar na documentação.
- Controles existentes:
  - rejeição de valores-exemplo no startup;
  - normalização de paths;
  - restrição de ferramentas por handler;
  - exclusão de `_agents/` da indexação pública;
  - assinatura de webhooks.

### `docs/decisions/README.md`

Índice dos ADRs.

Conteúdo esperado:

- O que é um ADR no projeto.
- Quando criar um ADR.
- Formato usado: contexto, decisão, consequências e status.
- Lista de decisões existentes.

### `docs/decisions/0001-documentation-architecture.md`

Primeiro ADR da documentação.

Conteúdo esperado:

- **Status:** Aceita.
- **Contexto:** README raiz cobre operação rápida, mas o projeto precisa de
  documentação técnica para manutenção e integração MCP.
- **Decisão:** organizar `docs/` em múltiplos arquivos por função de leitura,
  com Mermaid, C4 simplificado e ADRs.
- **Consequências:** documentação fica mais navegável e fácil de manter, mas
  mudanças arquiteturais passam a exigir atualização documental correspondente.

## Diagramas

Os diagramas devem usar Mermaid e permanecer pequenos o bastante para serem
revisados em diffs. A implementação deve priorizar:

- `flowchart` para contexto, containers e fluxos.
- `sequenceDiagram` para chamadas MCP e outbox quando a ordem temporal importar.
- `erDiagram` para modelo de dados simplificado.

Os diagramas devem descrever o estado atual do código. Quando uma simplificação
for necessária, o texto abaixo do diagrama deve explicar a simplificação.

## Regras De Qualidade

- Não usar marcadores de pendência nem seções vazias.
- Não incluir segredos reais, tokens ou valores da `.env`.
- Não documentar comportamento que o código atual não possui.
- Usar links relativos entre arquivos.
- Manter exemplos curtos e executáveis quando aplicável.
- Usar nomes reais de módulos, serviços e ferramentas do projeto.
- Separar contrato público de detalhe interno.
- Indicar explicitamente restrições de permissão e fronteiras de segurança.
- Preferir linguagem direta, sem marketing e sem generalidades.

## Critérios De Aceite

A implementação da documentação estará pronta quando:

- Todos os arquivos listados na estrutura existirem.
- `docs/README.md` permitir navegar para os demais documentos.
- `architecture.md` contiver pelo menos diagramas de contexto, containers,
  componentes e fluxos principais.
- `mcp-api.md` documentar as ferramentas públicas e de curadoria com contratos
  e exemplos.
- `development.md` permitir que um mantenedor configure o ambiente e rode os
  testes.
- `operations.md` cobrir deploy, healthchecks, worker, fila, outbox e backup.
- `data-model.md` distinguir fonte de verdade, índice derivado e registros
  operacionais.
- `security.md` documentar autenticação, permissões, segredos e riscos do inbox.
- `docs/decisions/` conter índice e primeiro ADR.
- Uma varredura simples não encontrar marcadores de pendência.

## Plano Posterior

Após aprovação deste spec, o próximo passo será criar um plano de implementação
com a skill `superpowers:writing-plans`, conforme o fluxo de brainstorming.
