# Operação

## Serviços

O ambiente de operação é definido pelo Docker Compose e separa banco, API, worker, proxy e backup em serviços distintos:

| Serviço | Função |
| --- | --- |
| `postgres` | Banco PostgreSQL da aplicação. Usa a imagem construída em `docker/postgres`, carrega `shared_preload_libraries=age`, persiste dados no volume `pgdata` e expõe healthcheck com `pg_isready -U brain`. |
| `api` | Serviço HTTP principal. Antes de subir o Uvicorn, executa `alembic upgrade head`; depois serve `brain.main:app` em `0.0.0.0:8000`. Monta `repo_cache` para manter o clone/cache do repositório monitorado. |
| `worker` | Processo assíncrono de fila. Executa `python -m brain.worker`, consome jobs do PostgreSQL, processa ingestão/fatos/exclusões e entrega eventos pendentes da outbox quando está ocioso. |
| `caddy` | Proxy reverso opcional, habilitado pelo profile `proxy`. Publica a API usando `BRAIN_DOMAIN` e portas HTTP/HTTPS configuráveis. |
| `backup` | Serviço opcional, habilitado pelo profile `backup`. Executa `docker/backup/backup.sh` e grava dumps customizados do PostgreSQL no volume `backups`. |

## Variáveis De Ambiente

Não coloque valores secretos em documentação, logs ou commits. As tabelas abaixo descrevem o que cada variável controla.

### Infra

| Variável | Controle |
| --- | --- |
| `POSTGRES_PASSWORD` | Senha do usuário PostgreSQL usado pelos serviços da aplicação. |
| `DATABASE_URL` | URL de conexão usada pela API, worker, migrações e componentes que acessam o banco. |
| `REPO_URL` | URL do repositório Git monitorado e indexado pelo Brain. |
| `REPO_CACHE_PATH` | Caminho local, dentro do contêiner, onde o repositório é clonado ou atualizado. |

### Autenticação e segredos

| Variável | Controle |
| --- | --- |
| `OPENAI_API_KEY` | Credencial para chamadas de embeddings e LLM. |
| `GITHUB_TOKEN` | Token usado para acessar e, quando habilitado, enviar alterações ao repositório GitHub. |
| `BRAIN_AUTH_TOKEN` | Token bearer exigido pelo endpoint operacional `GET /status`. |
| `BRAIN_CURATOR_TOKEN` | Token bearer do principal curador usado para acesso protegido ao `/mcp`. |
| `BRAIN_TOKEN_ENCRYPTION_KEY` | Chave Fernet usada para criptografar tokens de clientes de agente armazenados pela aplicação. |
| `WEBHOOK_SECRET` | Segredo compartilhado usado para validar a assinatura HMAC do webhook do GitHub. |

### Identidade do curador

| Variável | Controle |
| --- | --- |
| `BRAIN_CURATOR_SLUG` | Identificador estável do curador principal. |
| `BRAIN_CURATOR_NAME` | Nome legível do curador principal. |

### Caminhos de inbox de agentes

| Variável | Controle |
| --- | --- |
| `AGENT_INBOX_DIR` | Diretório reservado para inbox bruto de agentes. Em produção, mantenha `_agents` até que as fronteiras de indexação, busca e validação de caminhos sejam parametrizadas em todo o código. |
| `CONVERSATIONS_DIR` | Diretório usado para gravar conversas no repositório. |

### Git

| Variável | Controle |
| --- | --- |
| `GIT_PUSH_ENABLED` | Habilita ou desabilita operações que fazem push de mudanças para o repositório Git remoto. |

### Identidade Git

| Variável | Controle |
| --- | --- |
| `GIT_AUTHOR_NAME` | Nome de autor usado nos commits criados pela automação Git. |
| `GIT_AUTHOR_EMAIL` | Email de autor usado nos commits criados pela automação Git. |

### IA e indexação

| Variável | Controle |
| --- | --- |
| `EMBEDDING_MODEL` | Modelo usado para gerar embeddings dos documentos. |
| `EMBEDDING_DIM` | Dimensão esperada dos vetores de embedding armazenados. |
| `EXTRACTION_MODEL` | Modelo usado para extração de fatos. |
| `CHUNK_MAX_TOKENS` | Tamanho máximo, em tokens, de cada chunk indexado. |
| `CHUNK_OVERLAP_TOKENS` | Sobreposição, em tokens, entre chunks adjacentes. |

### Ajustes de fila e outbox

| Variável | Controle |
| --- | --- |
| `MAX_JOB_ATTEMPTS` | Número máximo de tentativas de processamento de um job antes de marcá-lo como falho. |
| `JOB_STALE_SECONDS` | Tempo usado para considerar um job em processamento como travado/stale e liberá-lo para nova tentativa. |
| `OUTBOX_MAX_ATTEMPTS` | Número máximo de tentativas de entrega de um evento da outbox antes de marcá-lo como falho. |

### Domínio público

| Variável | Controle |
| --- | --- |
| `BRAIN_DOMAIN` | Domínio público usado pelo Caddy para expor a API. |
| `BRAIN_HTTP_PORT` | Porta HTTP publicada pelo serviço `caddy`; o padrão operacional é `80`. |
| `BRAIN_HTTPS_PORT` | Porta HTTPS publicada pelo serviço `caddy`; o padrão operacional é `443`. |

### Hermes webhook

| Variável | Controle |
| --- | --- |
| `HERMES_WEBHOOK_URL` | URL de destino para eventos entregues pela outbox ao Hermes. Sem ela, a entrega externa fica desabilitada. |
| `HERMES_WEBHOOK_SECRET` | Segredo usado para assinar eventos enviados ao Hermes. Também precisa estar definido para a entrega externa ocorrer. |

### Backup

| Variável | Controle |
| --- | --- |
| `BRAIN_BACKUP_INTERVAL_SECONDS` | Intervalo entre execuções recorrentes do backup, em segundos. |
| `BRAIN_BACKUP_RETENTION_DAYS` | Janela de retenção dos arquivos de backup no volume `backups`, em dias. |
| `BRAIN_BACKUP_ONCE` | Flag opcional para executar um único backup e encerrar o serviço quando definida como `true`. |

## Deploy

Crie o arquivo de ambiente a partir do exemplo apenas se ele ainda não existir, para não sobrescrever configurações de produção. Depois, preencha os valores necessários antes de iniciar os serviços:

```bash
test -f .env || cp .env.example .env
docker compose build
docker compose up -d
curl http://localhost:8000/health
```

A resposta esperada do `/health`, quando a API e o banco estão operacionais, é:

```json
{"status":"ok","database":"ok"}
```

Para habilitar o proxy reverso com Caddy:

```bash
docker compose --profile proxy up -d
```

Para habilitar o serviço de backup recorrente:

```bash
docker compose --profile backup up -d backup
```

## Endpoints Operacionais

| Endpoint | Acesso | Uso |
| --- | --- | --- |
| `GET /health` | Público. | Healthcheck da API e do banco. Retorna `503` quando a verificação de banco falha. |
| `GET /status` | Requer `Authorization: Bearer $BRAIN_AUTH_TOKEN`. | Estado operacional autenticado da aplicação. |
| `POST /webhook/github` | Requer assinatura HMAC do GitHub em `X-Hub-Signature-256`. | Recebe eventos do GitHub e enfileira trabalho de indexação ou exclusão conforme os arquivos Markdown alterados. |
| `/mcp` | Protegido por tokens bearer de principal curador ou cliente. | Interface MCP da aplicação. Não usa `BRAIN_AUTH_TOKEN`; usa os tokens de principal apropriados. |

## Webhook Do GitHub

O endpoint `POST /webhook/github` processa eventos de push. Quando `X-GitHub-Event` está presente e não é `push`, o evento é ignorado sem disparar ingestão; webhooks reais do GitHub devem enviar `push`.

Cada requisição precisa trazer payload JSON válido e assinatura `X-Hub-Signature-256` compatível com `WEBHOOK_SECRET`. Assinaturas ausentes ou inválidas são rejeitadas.

Pushes feitos pelo autor de automação Git configurado são ignorados para evitar ciclos de ingestão causados por commits criados pela própria automação. Os valores padrão desse autor são `brain-bot` e `brain-bot@users.noreply.github.com`.

Quando um push válido chega, a API atualiza o clone/cache do repositório, normaliza os caminhos Markdown alterados e enfileira jobs conforme a mudança detectada. Arquivos Markdown adicionados ou modificados geram `index_document`; arquivos removidos geram `delete_document`.

Markdown sob `_agents/` é ignorado pelo caminho de webhook/indexação. Esses caminhos são rejeitados por `normalize_repo_path`, não geram jobs e aparecem nos logs como warning de caminho pulado (`webhook_skipped_repo_path`). Isso explica respostas com `enqueued: 0` quando o push altera apenas arquivos de inbox bruto.

## Worker E Fila

O worker consome a fila persistida no PostgreSQL e processa estes tipos de job:

| Job | Processamento |
| --- | --- |
| `index_document` | Indexa ou reindexa um documento Markdown no banco e nos índices derivados. |
| `reindex` | Reexecuta indexação para conteúdo já conhecido ou solicitado para reconstrução. |
| `delete_document` | Remove os registros associados a um documento excluído. |
| `extract_facts` | Extrai fatos estruturados a partir do conteúdo indexado. |

A fila reivindica jobs pendentes com bloqueio transacional e `FOR UPDATE SKIP LOCKED`, permitindo múltiplos consumidores sem processar o mesmo item simultaneamente. Falhas incrementam tentativas e reprogramam o job com backoff exponencial limitado a 300 segundos. Ao atingir o limite configurado por `MAX_JOB_ATTEMPTS`, o job é marcado como falho.

Durante períodos sem jobs disponíveis, o worker tenta entregar eventos pendentes da outbox. Aproximadamente a cada 60 segundos ociosos, ele também libera jobs travados/stale usando a janela configurada por `JOB_STALE_SECONDS`, permitindo nova tentativa por outro ciclo do worker.

## Outbox Para Hermes

A outbox envia eventos ao Hermes somente quando `HERMES_WEBHOOK_URL` e `HERMES_WEBHOOK_SECRET` estão definidos. Sem essas duas variáveis, os eventos podem permanecer sem entrega externa.

Cada entrega usa `HERMES_WEBHOOK_URL` como destino e assina o corpo com `HERMES_WEBHOOK_SECRET`. Os headers enviados incluem:

| Header | Uso |
| --- | --- |
| `X-Brain-Signature` | Assinatura HMAC calculada pelo Brain para validação no destino. |
| `X-Brain-Event-Type` | Tipo lógico do evento entregue. |
| `X-Brain-Event-Id` | Identificador único do evento da outbox. |
| `X-Brain-Timestamp` | Timestamp usado na assinatura e na validação temporal do evento. |
| `X-Hub-Signature-256` | Header de compatibilidade com consumidores que já validam assinaturas no formato do GitHub. |

Falhas de entrega usam retry com backoff exponencial limitado a 300 segundos. Ao atingir o limite configurado por `OUTBOX_MAX_ATTEMPTS`, o evento é marcado como falho.

## Backup E Restore

O profile `backup` inicia o serviço `backup`, que executa `docker/backup/backup.sh` e cria arquivos `brain-*.dump` no formato custom do `pg_dump` dentro do volume Docker `backups`. A recorrência é controlada por `BRAIN_BACKUP_INTERVAL_SECONDS` e a retenção por `BRAIN_BACKUP_RETENTION_DAYS`. Esses backups cobrem o banco PostgreSQL; eles não incluem volumes Docker como `repo_cache`, `caddy_data` ou `caddy_config`.

Mantenha backup e retenção separados do Git vault ou do volume `repo_cache`, especialmente quando `GIT_PUSH_ENABLED=false` ou quando pushes para Git falham. Arquivos Markdown brutos ou curados podem não ser recuperáveis apenas a partir do dump PostgreSQL.

Para executar um backup one-shot:

```bash
docker compose --profile backup run --rm -e BRAIN_BACKUP_ONCE=true backup
```

O restore dos arquivos `.dump` em formato custom deve ser feito com `pg_restore` em uma instância PostgreSQL compatível com as mesmas extensões usadas pela aplicação. O nome do arquivo segue o padrão do script de backup: `brain-YYYYmmddTHHMMSSZ.dump`. Antes de restaurar os dados da aplicação, instale pgvector e habilite a extensão SQL `vector`, além da extensão AGE. O arquivo `docker/postgres/init/01-extensions.sql` mostra as extensões esperadas; restaurar antes disso pode falhar por tipos, funções ou objetos dependentes ausentes.

Antes de qualquer restore destrutivo, siga uma sequência segura:

1. Selecione o arquivo correto em `/backups` e confira nome, timestamp e origem.
2. Prefira restaurar primeiro em um banco/volume novo ou em ambiente de staging.
3. Se o restore for em produção no banco existente, pare writers com `docker compose stop api worker backup`.
4. Confirme explicitamente que o alvo é o banco correto antes de executar `--clean --if-exists`.
5. Use `pg_restore --exit-on-error --single-transaction --clean --if-exists <arquivo.dump>`.
6. Depois do restore, suba os serviços e rode `curl http://localhost:8000/health`.

O exemplo abaixo restaura no serviço `postgres` existente do Compose e assume que o banco de destino já foi inicializado com as extensões esperadas.

Exemplo de template para restore:

```bash
docker compose --profile backup run --rm backup sh -c '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  pg_restore -h postgres -U brain -d brain --exit-on-error --single-transaction --clean --if-exists /backups/brain-YYYYmmddTHHMMSSZ.dump
'
```

Depois de restaurar:

```bash
docker compose up -d api worker
curl http://localhost:8000/health
docker compose --profile backup up -d backup
```

## Troubleshooting

| Sintoma | Causa provável | Ação |
| --- | --- | --- |
| `/health` retorna 503. | A API não consegue consultar o PostgreSQL, o banco ainda está iniciando ou `DATABASE_URL` está incorreta. | Verifique `docker compose ps`, logs de `postgres` e `api`, credenciais de banco e conectividade entre contêineres. |
| Webhook do GitHub retorna 401. | `WEBHOOK_SECRET` não corresponde ao segredo configurado no GitHub ou o header `X-Hub-Signature-256` está ausente/inválido. | Reconfigure o segredo nos dois lados e reenvie o evento de push assinado. |
| Worker não processa jobs. | Serviço `worker` parado, sem acesso ao banco, jobs travados ou exceções durante o processamento. | Verifique logs do worker, `DATABASE_URL`, saúde do `postgres` e se jobs stale estão sendo liberados. |
| Push para Git falha. | `GIT_PUSH_ENABLED` está desabilitado, `GITHUB_TOKEN` não tem permissão ou `REPO_URL` não permite escrita. | Confirme a flag, permissões do token, URL remota e logs da operação Git. |
| Chamadas de embedding ou LLM falham. | `OPENAI_API_KEY` ausente/inválida, limite de API ou erro externo do provedor. | Valide a chave, cotas/limites e logs da chamada; reexecute o job após corrigir a credencial ou aguardar recuperação. |
| Extensão AGE ou `vector` ausente. | Banco foi criado sem executar a imagem/init SQL esperados ou restore ocorreu em PostgreSQL incompatível. | Use a imagem de `docker/postgres` ou instale pgvector e habilite as extensões SQL `vector` e AGE antes de aplicar migrações/restaurar dados. |
| Eventos da outbox permanecem pendentes. | `HERMES_WEBHOOK_URL` ou `HERMES_WEBHOOK_SECRET` ausente, destino Hermes indisponível ou retries ainda em backoff. | Configure as variáveis, valide conectividade com Hermes, confira respostas HTTP e aguarde ou reprocesse após corrigir a causa. |

## Arquivos De Referência

- [docker-compose.yml](../docker-compose.yml)
- [Dockerfile](../Dockerfile)
- [Caddyfile](../Caddyfile)
- [docker/backup/backup.sh](../docker/backup/backup.sh)
- [docker/postgres/Dockerfile](../docker/postgres/Dockerfile)
- [docker/postgres/init/01-extensions.sql](../docker/postgres/init/01-extensions.sql)
- [src/brain/main.py](../src/brain/main.py)
- [src/brain/worker.py](../src/brain/worker.py)
- [src/brain/outbox.py](../src/brain/outbox.py)
- [src/brain/queue/postgres_queue.py](../src/brain/queue/postgres_queue.py)
