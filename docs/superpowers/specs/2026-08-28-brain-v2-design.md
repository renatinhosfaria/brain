# Brain V2 — Design Specification

**Status:** aprovado para planejamento e implementação  
**Data:** 2026-08-28  
**Fonte de requisitos:** `PRD.md` (V2, promovido de `PRD-v2.md`)  
**Escopo de desenvolvimento:** somente o repositório `/root/brain`

## 1. Objetivo

Ampliar o Brain V1 de memória longitudinal autorizada para um serviço local de
contexto autorizado da conversa. A V2 adiciona `conversation_phone()`, que
revela o telefone comprovado da conversa WhatsApp já determinada pelo runtime,
sem permitir que o modelo escolha a identidade.

O Brain continua sendo um serviço read-only, localhost-only e
capability-scoped. Ele não vira agente, CRM, roteador, FamaChat ou fonte
secundária do transcript.

## 2. Limites e invariantes

- Toda fonte de implementação, teste, exemplo de deploy e plugin fica em
  `/root/brain`.
- A instalação posterior em `/root/.hermes` é uma operação de deploy documentada,
  mas não é executada por esta implementação.
- Nenhum arquivo de `/usr/local/lib/hermes-agent` será modificado.
- O transcript canônico permanece no `state.db` do Hermes.
- O Brain nunca escreve `state.db`, `kanban.db` ou arquivos da sessão WhatsApp.
- Nenhum tool model-visible recebe telefone, LID, `chat_id`, `session_id`,
  `session_key`, Task, Run, Profile, caminho de banco ou caminho de mapping.
- Falha de autorização, inconsistência de sessão, mapping ausente, inválido ou
  ambíguo resulta em negação ou indisponibilidade; nunca em inferência.
- Logs não contêm telefone, LID, `chat_id`, `session_key`, `session_id`,
  transcript ou token.
- A regra comercial brasileira de remover código de país ou tratar o nono
  dígito permanece no Porteiro/Cadastro, não no Brain.

## 3. Migração documental e histórico Git

No commit de migração:

1. `PRD.md` V1 será movido para `PRD-v1.md`.
2. O conteúdo de `PRD-v2.md` será promovido para `PRD.md`.
3. O início de `PRD-v1.md` receberá a marca `HISTÓRICO — substituído pelo
   PRD.md V2`, sem alterar o conteúdo histórico restante.

Assim, `PRD.md` é a única especificação atual e o Git mantém a linhagem do
documento antigo por rename. `PRD-v2.md` não permanece como segunda fonte de
verdade no estado final.

## 4. Arquitetura

Há duas capacidades de execução que convergem para uma conversa autorizada:

```text
worker Kanban:  token + Task + Run + origem confiável
CEO gateway:    token da bridge + ContextVar da sessão corrente
                                  │
                                  ▼
                       AuthorizedConversation
                                  │
                                  ▼
                     resolver WhatsApp LID → phone
```

### 4.1 Worker Kanban

Os headers MCP continuam sendo:

```http
Authorization: Bearer ${BRAIN_TOKEN}
X-Hermes-Task: ${HERMES_KANBAN_TASK}
X-Hermes-Run: ${HERMES_KANBAN_RUN_ID}
```

O token identifica um principal worker. A autorização valida, nesta ordem,
antes de ler o transcript:

1. placeholder ou header ausente;
2. token e principal;
3. modo `worker` e allowlist do tool;
4. Task existente no board default, assignee igual ao principal e status
   `running`;
5. `current_run_id` igual ao Run apresentado;
6. Run existente, pertencente à Task e não terminal;
7. subscription WhatsApp com `task_id` correto, `platform=whatsapp`,
   `chat_type=dm`, `notifier_profile=default` e exatamente um `chat_id`;
8. `chat_id` resolve no `state.db` para sessões WhatsApp DM;
9. todas as sessões com a mesma `session_key` têm o mesmo vínculo de origem;
10. `task.session_id` pertence ao conjunto longitudinal resolvido.

A capability resultante é:

```python
WorkerCapability(
    principal="porteiro",
    task_id="...",
    run_id=123,
    chat_id="...",
    session_key="...",
    session_ids=("...", "..."),
)
```

`chat_id` é carregado do banco confiável e poderá ser usado internamente pelo
resolver. Ele nunca aparece no schema ou no resultado do tool.

### 4.2 CEO gateway

O CEO não possui Task/Run quando precisa resolver o telefone inicial. Um plugin
de usuário instalado separadamente captura a sessão corrente usando
`gateway.session_context.get_session_env()` e chama a rota privada do Brain.

O plugin deve ler, dentro de cada chamada:

- `HERMES_SESSION_PLATFORM`;
- `HERMES_SESSION_CHAT_TYPE`;
- `HERMES_SESSION_CHAT_ID`;
- `HERMES_SESSION_KEY`;
- `HERMES_SESSION_ID`;
- `HERMES_SESSION_PROFILE`.

Ele exige WhatsApp DM, contexto completo e o Profile CEO (`default`); não usa
`os.environ` como fonte primária. O token próprio da bridge autentica o caminho
gateway no Brain.

## 5. Componentes e contratos

### 5.1 Configuração (`src/brain/config.py`)

Substituir o hardcode de dois Profiles por principals dinâmicos:

```python
PrincipalConfig(
    name: str,
    mode: Literal["worker", "gateway"],
    token_sha256: str,
    tools: frozenset[str],
)
```

O TOML de produção terá entradas `[principals.<name>]`. A configuração de
referência usará:

```toml
[principals.default]
mode = "gateway"
token_sha256 = "..."
tools = ["conversation_phone"]

[principals.porteiro]
mode = "worker"
token_sha256 = "..."
tools = ["conversation_phone"]

[principals.cadastro]
mode = "worker"
token_sha256 = "..."
tools = ["conversation_phone"]

[principals.reno]
mode = "worker"
token_sha256 = "..."
tools = ["conversation_recent", "conversation_search"]

[principals.famaagent]
mode = "worker"
token_sha256 = "..."
tools = ["conversation_recent", "conversation_search"]
```

O principal `dev` não recebe acesso por padrão. Digests devem ser hexadecimais
de 64 caracteres, distintos. O caminho configurado
`whatsapp_session_dir` é server-side, deve existir como diretório e nunca pode
ser derivado de request.

### 5.2 Autorização (`src/brain/authorization.py`)

Separar os tipos:

```python
WorkerRequestIdentity(principal: str, task_id: str, run_id: int)
GatewayRequestIdentity(principal: str)
AuthorizedConversation(
    principal: str,
    mode: Literal["worker", "gateway"],
    source: Literal["whatsapp"],
    chat_type: Literal["dm"],
    chat_id: str,
    session_key: str,
    session_ids: tuple[str, ...],
)
```

O gateway não aceitará Task/Run e o worker não será aceito na rota gateway.
Ambos os caminhos retornam `AuthorizedConversation` para o service.

### 5.3 Resolver (`src/brain/whatsapp_identity.py`)

O módulo terá uma API pura e testável, sem importar runtime do Hermes durante o
atendimento:

```python
resolve_phone(chat_id: str, mapping_dir: Path) -> PhoneResolution
```

`PhoneResolution` será `ok` com `phone` ou `unavailable` com reason interno.

Regras:

- `^[1-9][0-9]{6,14}@s\.whatsapp\.net$` retorna os dígitos anteriores ao
  `@`, sem normalização comercial.
- `^[0-9]{1,20}@lid$` exige mapping; LID sem mapping retorna
  `PHONE_MAPPING_UNAVAILABLE`.
- grupos (`@g.us`), broadcast, caracteres especiais e JIDs desconhecidos não
  são tratados como telefone.
- somente arquivos exatamente no diretório configurado e que casem com
  `lid-mapping-[0-9]{7,15}.json` ou
  `lid-mapping-[0-9]{7,20}_reverse.json` são candidatos.
- não há `glob` construído a partir do input, `..`, symlink traversal,
  listagem, endpoint de arquivo ou leitura de `creds.json`/chaves.

Os formatos são interpretados semanticamente, não como um conjunto de aliases:

1. `lid-mapping-{phone}.json`: o `{phone}` do nome deve ser um telefone E.164
   numérico válido; o conteúdo JSON deve carregar um LID numérico único.
2. `lid-mapping-{lid}_reverse.json`: o `{lid}` do nome deve ser um LID numérico;
   o conteúdo JSON deve carregar um telefone E.164 numérico válido.

O parser aceitará os valores escalares e as estruturas de campo equivalentes
presentes no contrato atual, mas sempre validará tipo, unicidade e dígitos. Um
mapping malformado relevante ao alias falha fechado. Se os dois sentidos
existirem, eles precisam concordar. Um LID com dois telefones diferentes retorna
`PHONE_IDENTITY_AMBIGUOUS`; nunca se escolhe o menor, o mais curto ou o mais
brasileiro. `session_key` pode ser cross-check, mas nunca é fonte primária.

### 5.4 Service (`src/brain/service.py`)

Manter os dois tools V1 e adicionar:

```python
conversation_phone(capability: AuthorizedConversation) -> dict
```

Retornos públicos da ferramenta:

```json
{"status":"ok","phone":"5534999772714"}
```

ou:

```json
{"status":"unavailable","reason":"phone_not_resolved"}
```

Razões específicas de mapping não serão expostas ao modelo; ficam apenas no
evento de auditoria. O principal só pode chamar tools que estejam em sua ACL
server-side, mesmo que o cliente MCP tente contornar `tools.include`.

### 5.5 MCP (`src/brain/mcp_server.py`)

`conversation_phone` terá schema:

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

O servidor lista três tools: `conversation_recent`, `conversation_search` e
`conversation_phone`. A filtragem por Profile ocorre no Hermes via allowlist e
a autorização definitiva ocorre dentro do Brain.

### 5.6 Rota privada (`src/brain/gateway_api.py`)

Adicionar `POST /internal/gateway/conversation-phone`, acessível somente no
bind localhost. O endpoint exige o token do principal gateway no header
`Authorization: Bearer ...` e rejeita placeholders, headers ausentes e token de
worker.

O corpo aceito é exatamente:

```json
{
  "platform": "whatsapp",
  "chat_type": "dm",
  "chat_id": "123456789012345@lid",
  "session_key": "...",
  "session_id": "..."
}
```

O Brain consulta o `state.db` e exige que `session_id` exista e tenha exatamente
o `source`/plataforma, `chat_type`, `chat_id` e `session_key` enviados. O corpo
não é uma declaração de identidade confiável; é uma referência que precisa ser
revalidada. Divergência retorna erro genérico ou indisponibilidade, sem ecoar
qualquer campo.

### 5.7 Plugin (`integrations/hermes/brain-ceo-bridge/`)

Arquivos versionados:

```text
plugin.yaml
__init__.py
schemas.py
tools.py
README.md
```

O plugin registra somente `conversation_phone` no toolset `brain-context`,
declara `requires_env: [BRAIN_GATEWAY_TOKEN]` e não solicita capabilities
adicionais. O handler:

- usa `get_session_env` em cada chamada;
- não usa `os.getenv` para identidade corrente;
- não aceita argumentos;
- envia somente a rota fixa localhost com timeout curto;
- retorna sempre JSON sanitizado e nunca levanta para o modelo;
- não registra o payload bruto.

A cópia para `/root/.hermes/plugins/brain-ceo-bridge` e a habilitação em
`config.yaml` serão descritas no runbook, mas não feitas nesta tarefa.

## 6. Health, auditoria e disponibilidade

O health permanece em `/health` e sem PII:

```json
{
  "status": "ok",
  "hermes_state_db": "ok",
  "hermes_kanban_db": "ok",
  "whatsapp_identity": "compatible",
  "gateway_bridge": "configured",
  "schema": "compatible"
}
```

HTTP 503 será usado quando banco/schema, diretório de identidade ou principal
gateway não estiverem compatíveis.

Auditoria inclui timestamp, principal, modo, tool, decisão, código interno,
latência e contagens. Nunca inclui PII. Indisponibilidade da resolução de
telefone é um resultado funcional controlado; erro de autorização é genérico;
nenhum caminho faz fallback para nome, texto da mensagem, `session_key` ou
consulta comercial.

## 7. Configuração e documentação versionada

Atualizar no repositório:

- `README.md`: definição V2, três tools, principals, plugin e separação entre
  desenvolvimento e instalação.
- `docs/runbook.md`: provisioning de cinco credenciais distintas, instalação da
  bridge, configuração de Porteiro/Cadastro, teste LID/concorrência, health,
  update gate e rollback.
- `docs/worker-history-invariant.md`: manter a regra de histórico como evidência.
- `docs/conversation-identity-invariant.md`: registrar que o runtime determina a
  conversa e o Brain apenas revela o telefone comprovado.
- `deploy/brain.toml.example`: principals dinâmicos e
  `whatsapp_session_dir`.
- `deploy/hermes-brain.example.yaml`: blocos separados para Porteiro/Cadastro,
  configuração V1 preservada para Reno/FamaAgent e instruções de CEO plugin;
  Telegram/WhatsApp dos workers continuam com `no_mcp`.
- `deploy/brain.service`: descrição atualizada sem sugerir que o serviço só
  atende memória.
- scripts de secrets, integration check e smoke test: novo principal gateway,
  allowlists, plugin, health e três tools.

Não atualizar configurações vivas sob `/root/.hermes` nesta implementação.

## 8. Testes e critérios de aceite

Todos os testes novos seguem RED → GREEN → REFACTOR; cada teste deve falhar
antes do código de produção correspondente.

### Resolver

- JID telefônico válido retorna dígitos;
- LID com mapping normal retorna o telefone do nome do arquivo;
- reverse mapping retorna o telefone do conteúdo;
- dois sentidos consistentes passam;
- ausência, JSON malformado, telefone/LID inválido e path traversal falham
  fechado;
- dois telefones para o mesmo LID retornam ambiguidade;
- grupo, broadcast e `session_key` sozinho nunca resolvem telefone.

### Worker e gateway

- Porteiro e Cadastro autorizados usam `conversation_phone`;
- Reno/FamaAgent mantêm recent/search e não ganham phone por padrão;
- cross-profile, cross-mode, task/run antigo, Task não running, Telegram, grupo
  e subscription ambígua são negados;
- gateway aceita somente contexto WhatsApp DM revalidado;
- token worker na rota gateway e token gateway no MCP worker são negados;
- sessão divergente, chave divergente e contexto ausente falham fechado.

### Concorrência e superfície

- centenas de chamadas simultâneas com duas `ContextVar`s não trocam A/B;
- schema de `conversation_phone` não expõe argumentos;
- logs de sucesso, deny e unavailable não contêm token ou PII;
- `conversation_recent` e `conversation_search` preservam os 42 testes V1;
- health incompatível retorna 503;
- integration/smoke tests verificam plugin, `no_mcp`, allowlists e contrato
  `get_session_env` sem modificar o Hermes instalado.

## 9. Sequência de implementação

1. Migrar os documentos e atualizar a configuração tipada sem regressão V1.
2. Generalizar autorização e incluir `chat_id` na capability worker.
3. Implementar o resolver sem dependência de runtime Hermes.
4. Adicionar `conversation_phone`, ACL e schema MCP.
5. Adicionar a rota gateway com revalidação no `state.db`.
6. Criar e testar o plugin versionado.
7. Atualizar scripts, exemplos, health e documentação.
8. Rodar suíte completa, lint e verificações de contrato.

O deploy fora do repositório — cópia para `/root/.hermes`, alteração de
`config.yaml`, secrets reais e validação com LID de produção — é uma etapa
posterior documentada, não parte das mutações desta sessão.
