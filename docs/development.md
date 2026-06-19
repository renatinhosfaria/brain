# Desenvolvimento

## Requisitos

- Python 3.12 ou mais recente.
- `uv`.
- Docker.
- Git.
- Acesso a uma chave de API compatível com OpenAI para chamadas reais de embeddings e extração.

## Setup Local

Prepare a imagem local do Postgres com pgvector e Apache AGE, instale as dependências e crie o arquivo de ambiente:

```bash
docker build -t brain-postgres:local docker/postgres
uv sync
test -f .env || cp .env.example .env
```

O arquivo `.env` deve usar somente segredos locais ou de desenvolvimento. Não faça commit desse arquivo nem de valores sensíveis derivados dele. O `.env.example` contém valores de exemplo que `Settings` rejeita na inicialização; substitua esses valores antes de executar a aplicação. Gere uma chave Fernet válida para `BRAIN_TOKEN_ENCRYPTION_KEY` e configure `OPENAI_API_KEY` para chamadas reais de embeddings e extração.

## Testes

Execute a suíte completa com:

```bash
uv run pytest
```

Comandos úteis para ciclos focados:

```bash
uv run pytest tests/test_config.py -q
uv run pytest tests/integration/test_mcp_handlers.py -q
uv run pytest tests/integration/test_graph.py -q
```

- Testes unitários ficam em `tests/`.
- Testes de integração ficam em `tests/integration/`.
- Testes de integração com Postgres dependem da imagem Docker local com pgvector e AGE.
- Mudanças em MCP, fila, worker, repositórios e AGE precisam de cobertura de integração.

## Estrutura Do Código

- `src/brain/main.py`: define a aplicação FastAPI, `/health`, `/status`, webhook do GitHub, montagem do MCP e construção das dependências.
- `src/brain/auth.py`: modela principals, resolução de bearer token e geração, hash e criptografia de tokens de cliente.
- `src/brain/config.py`: centraliza configurações Pydantic e rejeita valores inseguros de exemplo na inicialização.
- `src/brain/mcp/server.py`: registra ferramentas FastMCP e expõe schemas públicos.
- `src/brain/mcp/handlers.py`: aplica autorização por principal, valida entradas das ferramentas e orquestra repositório, Git, busca e fila.
- `src/brain/repo_paths.py`: normaliza caminhos relativos ao vault e bloqueia caminhos inseguros para notas curated/indexadas, como `_agents/`.
- `src/brain/storage/models.py`: declara os modelos ORM do SQLAlchemy.
- `src/brain/storage/repositories.py`: concentra funções de persistência, limites de busca e validação de prefixo de caminho.
- `src/brain/ingestion/pipeline.py`: executa indexação, chunking, embeddings, extração de entidades e fatos e escrita no grafo.
- `src/brain/ingestion/git_writer.py`: renderiza Markdown, escreve notas curated/raw e controla commit/push no Git.
- `src/brain/search/retriever.py`: orquestra os contratos `search` e `deep_search`.
- `src/brain/graph/age.py`: encapsula operações no grafo Apache AGE.
- `src/brain/queue/`: define a interface de fila e a implementação Postgres.
- `src/brain/outbox.py`: entrega webhooks Hermes a partir do outbox.
- `src/brain/worker.py`: executa o loop de claim e tratamento de jobs.

## Como Adicionar Uma Ferramenta MCP

1. Defina o handler em `src/brain/mcp/handlers.py`, mantendo o contrato público explícito e validando entradas na borda.
2. Aplique a autorização do principal no handler antes de tocar em repositórios, Git, busca ou fila.
3. Registre a ferramenta em `src/brain/mcp/server.py`, incluindo schema público coerente com o handler.
4. Adicione ou atualize testes de integração em `tests/integration/test_mcp_handlers.py`.
5. Inspecione também `tests/integration/test_retriever.py` quando a ferramenta consultar busca ou alterar o contrato de `search`.

## Como Adicionar Uma Migration

1. Atualize `src/brain/storage/models.py` quando a mudança alterar o modelo de dados usado pela aplicação.
2. Crie uma revisão Alembic com operações explícitas de upgrade e downgrade quando aplicável.
3. Verifique que a migration preserva dados existentes e funciona em banco limpo.
4. Execute testes de upgrade e migração, usando `tests/integration/test_migrations.py` como referência.

## Como Adicionar Um Job De Worker

1. Adicione o novo tipo de job ao contrato da fila em `src/brain/queue/`.
2. Implemente o caminho de tratamento no worker em `src/brain/worker.py`.
3. Cubra enfileiramento, claim, conclusão e falha com testes de fila e worker.
4. Defina o comportamento de retry, incluindo quando a falha deve ser reprocessada ou marcada como definitiva.
5. Use `tests/integration/test_worker.py` para orientar a cobertura de integração.

## Como Alterar Busca Ou Grafo

1. Mantenha estável o contrato público de `search`; quando for inevitável alterá-lo, atualize handlers, schemas e documentação juntos.
2. Adicione testes para o retriever em `tests/integration/test_retriever.py`.
3. Adicione testes de integração AGE em `tests/integration/test_graph.py` para mudanças em `src/brain/graph/age.py`.
4. Verifique efeitos em ferramentas MCP que expõem busca pública, principalmente `tests/integration/test_mcp_handlers.py`.

## Convenções De Manutenção

- Prefira os limites de módulos existentes.
- Mantenha contratos públicos em handlers explícitos.
- Mantenha funções de repositório focadas em persistência.
- Mantenha a validação de `repo_path` centralizada por `normalize_repo_path`.
- Não indexe `_agents/`.
- Não exponha `memories` pela busca MCP pública.
- Use testes de integração ao tocar em DB, fila, worker, MCP ou AGE.
- Atualize a documentação ao mudar arquitetura, contratos públicos MCP, deployment, modelo de dados ou comportamento de segurança.

## Arquivos De Referência

- [tests/integration/test_mcp_handlers.py](../tests/integration/test_mcp_handlers.py)
- [tests/integration/test_retriever.py](../tests/integration/test_retriever.py)
- [tests/integration/test_graph.py](../tests/integration/test_graph.py)
- [tests/integration/test_worker.py](../tests/integration/test_worker.py)
- [tests/integration/test_migrations.py](../tests/integration/test_migrations.py)
