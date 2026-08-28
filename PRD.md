# Brain — Product Requirements Document (PRD)

**Versão:** 2.0  
**Status:** Proposta técnica pronta para implementação  
**Data:** 2026-08-28  
**Produto:** Brain  
**Repositório:** `renatinhosfaria/brain`  
**Objetivo desta revisão:** ampliar o Brain de “memória longitudinal autorizada para workers” para “contexto autorizado da conversa”, adicionando resolução segura do telefone da conversa WhatsApp para o CEO, Porteiro e Cadastro, sem modificar o core do Hermes Agent.

---

## 1. Resumo executivo

O Brain atual é um serviço MCP local, somente leitura, que permite a workers do Hermes recuperar o histórico da conversa WhatsApp que originou a Task Kanban atual.

A V2 deve acrescentar uma nova capacidade:

```text
conversation_phone()
```

Essa ferramenta deve responder:

> “Qual é o telefone comprovadamente associado à conversa WhatsApp que esta execução está autorizada a acessar?”

O modelo nunca deve fornecer telefone, LID, `chat_id`, `session_id`, `session_key`, Task, Run ou Profile para escolher a identidade.

Para workers Kanban, a autorização existente baseada em **Profile + Task + Run + origem Kanban** será reutilizada.

Para o CEO existe uma diferença estrutural: quando uma nova mensagem chega pelo WhatsApp, ele precisa resolver a identidade **antes de existir a primeira Task**. O Hermes mantém a identidade da sessão atual em `ContextVar` task-local, enquanto a configuração MCP comum interpola valores de environment/profile e não oferece um header de “sessão gateway atual”.

Por isso, a arquitetura aprovada para a V2 é:

- workers (`porteiro`, `cadastro`, `reno`, `famaagent`) acessam o Brain diretamente via MCP conforme a necessidade de cada Profile;
- o CEO acessa a mesma capacidade do Brain através de um **plugin de usuário mínimo do Hermes**, instalado fora do core;
- esse plugin não resolve telefone e não toma decisão;
- o plugin apenas captura o contexto da conversa atual através da API pública `gateway.session_context.get_session_env()` e o envia ao Brain por localhost;
- o Brain continua sendo a única camada responsável por validar a conversa e resolver o telefone;
- nenhuma alteração será feita no código-fonte do Hermes Agent;
- `hermes update` continuará podendo substituir/atualizar o core sem apagar a implementação do Brain ou o plugin de usuário.

---

## 2. Problema de negócio

A Fama identifica quem está falando pelo telefone.

O fluxo inicial é:

```text
WhatsApp
   ↓
CEO
   ↓
Porteiro
   ├── telefone em sistema_users ativo → corretor
   └── não encontrou → Cadastro
                         ├── telefone em clientes → cliente
                         └── não encontrou → novo lead
```

Porteiro e Cadastro já possuem regras próprias para comparar telefones brasileiros:

1. deixar somente dígitos;
2. remover `55` quando presente;
3. comparar;
4. quando necessário, tratar a diferença do nono dígito.

Essas regras pertencem à camada de negócio FamaChat e devem continuar nos Profiles.

### 2.1 Falha observada

O WhatsApp pode entregar uma pessoa por um identificador de privacidade chamado LID, por exemplo:

```text
999999999999999@lid
```

em vez do formato telefônico:

```text
5534999772714@s.whatsapp.net
```

Quando isso ocorreu, o CEO viu apenas o nome de exibição. O telefone não foi exposto ao modelo.

CEO e Porteiro agiram corretamente: não inferiram o telefone.

O dado, porém, existe localmente. O bridge do WhatsApp mantém arquivos que relacionam LID e telefone, e o Hermes já usa essa relação para canonicalizar a identidade das sessões.

O problema é portanto de **acesso seguro a um dado já existente**, e não de falta de dado.

---

## 3. Estado atual verificado

### 3.1 Hermes Agent

A documentação e o código atual confirmam:

- conversas de gateway são armazenadas em `~/.hermes/state.db`;
- WhatsApp DM usa uma chave de sessão com `canonical_identifier`;
- aliases LID/telefone são colapsados quando existe mapping;
- o bridge persiste sessão em `~/.hermes/platforms/whatsapp/session`;
- `gateway/whatsapp_identity.py` contém helpers públicos de identidade WhatsApp;
- `scripts/whatsapp-bridge/bridge.js` monta um mapa LID → telefone a partir de arquivos `lid-mapping-{phone}.json`;
- o gateway mantém dados da sessão corrente em `ContextVar`, incluindo:
  - `HERMES_SESSION_PLATFORM`
  - `HERMES_SESSION_CHAT_ID`
  - `HERMES_SESSION_CHAT_TYPE`
  - `HERMES_SESSION_USER_ID`
  - `HERMES_SESSION_USER_ID_ALT`
  - `HERMES_SESSION_KEY`
  - `HERMES_SESSION_ID`
  - `HERMES_SESSION_PROFILE`
- `get_session_env()` é a API pública prevista para ferramentas lerem esse contexto com segurança em execução concorrente;
- workers Kanban recebem em environment:
  - `HERMES_KANBAN_TASK`
  - `HERMES_KANBAN_RUN_ID`;
- `kanban.auto_subscribe_on_create: true` preserva a origem da Task e acorda a sessão criadora após eventos terminais;
- plugins de usuário são a extensão suportada para ferramentas próprias sem editar o core;
- plugins de usuário vivem em `$HERMES_HOME/plugins/` e são habilitados por `plugins.enabled`;
- MCP HTTP suporta headers e interpolação de environment/profile;
- `identity_header` identifica o Profile, mas não transporta a sessão gateway atual.

### 3.2 Brain V1

O Brain atual:

- roda em `127.0.0.1:8765`;
- é read-only;
- abre `state.db` e `kanban.db` em modo somente leitura;
- possui exatamente dois tools:
  - `conversation_recent`
  - `conversation_search`;
- autentica Profile por token;
- recebe Task e Run pelos headers MCP;
- valida a Task, o Run e a assinatura/origem WhatsApp;
- deriva a conversa autorizada;
- impede seleção model-visible de:
  - `phone`
  - `chat_id`
  - `session_id`
  - `session_key`
  - `task_id`
  - `run_id`
  - `profile`
  - `conversation_id`;
- atualmente aceita exatamente `reno` e `famaagent`;
- atualmente não resolve LID → telefone;
- atualmente não oferece acesso ao CEO;
- atualmente considera Brain ausente do CEO como requisito explícito no smoke/integration check.

### 3.3 Profiles atuais

Profiles operacionais encontrados:

- `default` — CEO;
- `porteiro`;
- `cadastro`;
- `reno`;
- `famaagent`;
- `dev`.

Brain V1 já está configurado em:

- `reno`;
- `famaagent`.

Brain ainda não está configurado em:

- CEO;
- Porteiro;
- Cadastro.

---

## 4. Decisão de produto

A V2 redefine o Brain como:

> **Serviço local, read-only e capability-scoped que fornece contexto confiável da conversa autorizada aos agentes Hermes.**

“Contexto confiável” inclui:

1. histórico longitudinal autorizado;
2. telefone comprovado da conversa WhatsApp autorizada.

O Brain **não** vira agente, CRM, sistema de cadastro, FamaChat, roteador ou ferramenta de busca global.

---

## 5. Princípios e invariantes

### 5.1 O modelo nunca escolhe identidade

Nenhuma ferramenta model-visible poderá receber:

- telefone;
- LID;
- `chat_id`;
- `session_id`;
- `session_key`;
- `task_id`;
- `run_id`;
- Profile;
- caminho de banco;
- caminho do diretório WhatsApp;
- identificador de conversa equivalente.

`conversation_phone()` terá **zero argumentos de identidade**.

### 5.2 Identidade vem de provenance confiável

Workers:

```text
token do Profile
     +
Task
     +
Run
     +
origem Kanban confiável
     ↓
conversa autorizada
```

CEO:

```text
sessão WhatsApp corrente
capturada dentro do Hermes
por ContextVar
     +
credencial exclusiva da bridge
     ↓
conversa autorizada
```

### 5.3 O Brain resolve; o plugin só transporta contexto

O plugin do CEO:

- não lê arquivos `lid-mapping`;
- não interpreta LID;
- não extrai telefone de `session_key`;
- não normaliza telefone;
- não consulta FamaChat.

Ele somente envia ao Brain o contexto corrente que o próprio Hermes já vinculou ao turno.

### 5.4 Fail closed

Se houver:

- falta de mapping;
- mapping inválido;
- múltiplos telefones possíveis;
- origem não WhatsApp;
- grupo em vez de DM;
- sessão divergente;
- Profile divergente;
- Task/Run inválidos;
- arquivo ilegível;
- contrato do Hermes incompatível;

o Brain não infere.

Resposta funcional:

```json
{
  "status": "unavailable",
  "reason": "phone_not_resolved"
}
```

Falha de autorização continua sendo erro genérico, sem revelar o motivo ao modelo além do necessário.

### 5.5 Brain não faz normalização de negócio

O Brain deve devolver o telefone de transporte comprovado, em dígitos, preservando o código do país quando presente.

Exemplo:

```json
{
  "status": "ok",
  "phone": "5534999772714"
}
```

O Brain não deve:

- retirar `55` por regra FamaChat;
- inserir `55`;
- remover o nono dígito;
- criar variações;
- decidir que dois telefones brasileiros são equivalentes.

Essa lógica continua no Porteiro/Cadastro.

### 5.6 PII mínima

O telefone pode sair apenas no resultado de `conversation_phone()` para um principal autorizado.

Nunca registrar em logs:

- telefone;
- LID;
- `chat_id`;
- `session_key`;
- `session_id`;
- mensagem bruta.

---

## 6. Objetivos funcionais

### O1 — CEO resolve telefone antes do primeiro cartão

Em uma WhatsApp DM, o CEO deve poder chamar:

```text
conversation_phone()
```

sem parâmetros e receber o telefone da conversa atual.

### O2 — Porteiro resolve telefone pela origem da Task

Quando executado por Kanban, Porteiro deve poder chamar a mesma capacidade sem fornecer identidade.

### O3 — Cadastro resolve telefone pela origem da Task

Cadastro deve ter a mesma capacidade.

### O4 — Reno/FamaAgent preservam memória V1

`conversation_recent` e `conversation_search` continuam funcionando sem regressão.

### O5 — Não modificar Hermes core

Toda integração com o turno vivo do CEO será feita via plugin de usuário.

### O6 — Sobreviver a `hermes update`

Após uma atualização:

- Brain continua em `/root/brain`;
- plugin continua fora do core;
- smoke tests verificam compatibilidade;
- falha de compatibilidade desabilita a nova capacidade em vez de inferir.

---

## 7. Não objetivos

Fora do escopo:

- identificar corretor dentro do Brain;
- consultar `sistema_users`;
- consultar clientes;
- cadastrar lead;
- decidir roteamento;
- responder WhatsApp;
- gravar `state.db`;
- gravar `kanban.db`;
- gravar arquivos de sessão WhatsApp;
- alterar o bridge do Hermes;
- alterar `gateway/session_context.py`;
- alterar `gateway/whatsapp_identity.py`;
- parsear o texto da mensagem para descobrir telefone;
- usar nome de exibição como identidade;
- busca arbitrária por telefone;
- aceitar `phone` como input;
- grupos WhatsApp;
- Telegram como fonte de telefone;
- autorizar automaticamente todo Profile existente.

---

## 8. Arquitetura alvo

```text
                         ┌──────────────────────┐
WhatsApp ───────────────►│ Hermes Gateway / CEO│
                         └──────────┬───────────┘
                                    │
                             conversation_phone()
                                    │
                         plugin de usuário Hermes
                         captura ContextVar atual
                                    │ localhost
                                    ▼
                           ┌──────────────────┐
                           │      Brain       │
                           │                  │
                           │ autorização      │
                           │ conversa         │
                           │ LID → telefone   │
                           │ histórico        │
                           └───────┬──────────┘
                                   │ read-only
                ┌──────────────────┼───────────────────┐
                ▼                  ▼                   ▼
          state.db           kanban.db       whatsapp/session
                                                │
                                      lid-mapping-*.json


CEO cria Task
     │
     ├────────► Porteiro worker ── MCP ──► Brain
     │                                  conversation_phone()
     │
     ├────────► Cadastro worker ── MCP ──► Brain
     │                                  conversation_phone()
     │
     ├────────► Reno worker ────── MCP ──► Brain
     │                                  recent/search
     │
     └────────► FamaAgent worker ─ MCP ──► Brain
                                        recent/search
```

---

## 9. Dois caminhos de autorização

### 9.1 Worker capability — manter e generalizar

O caminho atual continua:

```http
Authorization: Bearer ${BRAIN_TOKEN}
X-Hermes-Task: ${HERMES_KANBAN_TASK}
X-Hermes-Run: ${HERMES_KANBAN_RUN_ID}
```

Brain valida:

1. token identifica um principal worker;
2. Task existe;
3. assignee da Task é o Profile autenticado;
4. Task está `running`;
5. `current_run_id` coincide;
6. Run existe e não é terminal;
7. origem WhatsApp DM existe;
8. há exatamente um `chat_id`;
9. a sessão da Task pertence àquela conversa;
10. a capability é criada.

A capability V2 precisa incluir também:

```python
chat_id: str
```

Além de `session_key` e `session_ids`.

### 9.2 Gateway capability — novo caminho para CEO

O CEO não possui Task/Run antes do primeiro cartão.

A configuração MCP normal não é suficiente para provar a sessão corrente com segurança porque o gateway usa `ContextVar` task-local para evitar mistura entre conversas concorrentes.

Será criado um plugin de usuário Hermes chamado, provisoriamente:

```text
brain-ceo-bridge
```

Ele registra um toolset:

```text
brain-context
```

e uma ferramenta zero-argumento:

```text
conversation_phone()
```

O handler:

1. importa `get_session_env` de `gateway.session_context`;
2. lê somente:
   - plataforma;
   - tipo de chat;
   - `chat_id`;
   - `session_key`;
   - `session_id`;
   - Profile;
3. exige:
   - `platform == "whatsapp"`;
   - `chat_type == "dm"`;
   - Profile autorizado para gateway;
4. envia esse contexto para um endpoint privado localhost do Brain;
5. devolve ao modelo apenas o payload sanitizado do Brain.

O plugin **não** aceita argumentos de identidade.

---

## 10. Endpoint privado para a bridge do CEO

Adicionar ao serviço Brain:

```text
POST /internal/gateway/conversation-phone
```

Bind continua exclusivamente localhost.

### 10.1 Autenticação

A bridge usa um token próprio:

```text
BRAIN_GATEWAY_TOKEN
```

Separado dos tokens de workers.

O Brain armazena apenas SHA-256/digest, seguindo a política atual.

### 10.2 Corpo interno

Exemplo de corpo produzido pelo plugin, nunca pelo modelo:

```json
{
  "platform": "whatsapp",
  "chat_type": "dm",
  "chat_id": "123456789012345@lid",
  "session_key": "agent:main:whatsapp:dm:5534999772714",
  "session_id": "..."
}
```

### 10.3 Revalidação server-side

O Brain não confiará somente no payload da bridge.

Deve consultar `state.db` e confirmar:

- `session_id` existe;
- source/plataforma é WhatsApp;
- `chat_type` é DM;
- `chat_id` do banco coincide;
- `session_key` do banco coincide.

Divergência → deny/fail closed.

### 10.4 Resposta

Sucesso:

```json
{
  "status": "ok",
  "phone": "5534999772714"
}
```

Indisponível:

```json
{
  "status": "unavailable",
  "reason": "phone_not_resolved"
}
```

Nunca devolver:

- LID;
- `chat_id`;
- `session_id`;
- `session_key`;
- caminho de arquivo;
- nome de arquivo de mapping.

---

## 11. Nova ferramenta MCP `conversation_phone`

Adicionar um terceiro tool ao MCP Brain:

```text
conversation_phone
```

Schema:

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

Sem argumentos.

`BrainService.call_tool()` deve:

1. autenticar;
2. validar ACL do principal;
3. reconstruir capability;
4. chamar o resolvedor de telefone sobre `capability.chat_id`;
5. retornar apenas o contrato público.

---

## 12. Resolvedor WhatsApp LID → telefone

Criar módulo próprio do Brain, por exemplo:

```text
src/brain/whatsapp_identity.py
```

Ele não deve importar runtime interno do Hermes durante o atendimento.

O Brain deve reproduzir somente o contrato de dados mínimo de que precisa e verificar esse contrato no smoke test pós-update.

### 12.1 Diretório configurável

Adicionar configuração:

```toml
[server]
whatsapp_session_dir = "/root/.hermes/platforms/whatsapp/session"
```

O caminho nunca é model-visible.

### 12.2 Caso telefone JID direto

Se o `chat_id` confiável for:

```text
5534999772714@s.whatsapp.net
```

o telefone comprovado é:

```text
5534999772714
```

desde que o identificador seja estritamente válido.

### 12.3 Caso LID

Se for:

```text
123456789012345@lid
```

o Brain procura mapping conhecido.

O bridge atual constrói LID → telefone lendo arquivos:

```text
lid-mapping-{phone}.json
```

onde o nome do arquivo carrega o telefone e o conteúdo contém o LID.

O resolvedor deve aceitar apenas associação comprovada e única.

### 12.4 Reverse mappings

O Hermes também contempla arquivos `_reverse`.

O Brain deve suportar os formatos atuais verificados em produção/upstream, mas nunca assumir que um número “parece telefone”.

### 12.5 Ambiguidade

Se um LID resolver para mais de um telefone diferente:

```text
status = unavailable
reason = identity_ambiguous
```

Nunca escolher “o menor”, “o mais curto”, “o que parece brasileiro” ou equivalente.

### 12.6 Ausência de mapping

Um LID numérico sem mapping **não é telefone**.

Retornar indisponível.

### 12.7 `session_key` é cross-check, não fonte primária

O Hermes atual usa `canonical_identifier` no `session_key`, mas o Brain não deve depender do formato textual da chave como única prova.

Pode comparar o candidato resolvido com a parte canônica esperada como defesa adicional.

Não deve extrair telefone de `session_key` e aceitar sozinho.

---

## 13. Autorização por principal e ACL de tools

A configuração atual exige exatamente `reno` e `famaagent`.

Isso deve ser removido.

Introduzir principals configuráveis, com modo e tools permitidos.

Exemplo conceitual:

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

`dev` permanece sem acesso por padrão.

### 13.1 Defense in depth

`tools.include` do Hermes não é a única contenção.

O Brain deve recusar server-side uma ferramenta não autorizada ao principal.

Exemplo:

- token Porteiro tentando `conversation_search` → deny;
- token Reno tentando `conversation_phone` → deny, salvo se explicitamente autorizado em configuração;
- token gateway tentando rota worker → deny;
- token worker tentando endpoint gateway → deny.

---

## 14. Uso por Profile

### 14.1 CEO

Primário:

```text
conversation_phone()
```

Antes de criar cartão de identidade em WhatsApp DM.

Se `status=ok`, o CEO inclui no cartão:

```yaml
contact:
  phone_e164: "5534999772714"
```

O nome `phone_e164` pode ser preservado por compatibilidade, mas o valor retornado pelo Brain é “phone digits proven by WhatsApp”; o Brain não faz normalização comercial.

Se indisponível:

- não inferir;
- não usar nome de exibição;
- preservar o comportamento fail-safe;
- o cartão, se criado, deve declarar explicitamente que a resolução pelo CEO falhou e que o worker deve tentar sua própria capability Brain antes de bloquear.

### 14.2 Porteiro

Adicionar Brain MCP em CLI com:

```text
conversation_phone
```

Novo contrato operacional:

1. se cartão contém telefone, pode seguir o fluxo existente;
2. se cartão não contém telefone:
   - chamar `conversation_phone()`;
   - se `ok`, usar o telefone somente durante a execução;
   - se indisponível, bloquear;
3. nunca colocar telefone no `summary` ou `metadata`;
4. normalização brasileira continua no Porteiro;
5. consulta FamaChat continua em `fc_get_users`.

### 14.3 Cadastro

Mesma regra:

1. telefone do cartão é preferido quando presente;
2. se ausente, resolver via Brain;
3. se indisponível, bloquear;
4. regras de matching e nono dígito continuam no Cadastro;
5. Brain não consulta clientes;
6. criação continua no FamaChat.

### 14.4 Reno

Manter:

- `conversation_recent`;
- `conversation_search`.

Não habilitar `conversation_phone` por padrão na V2, a menos que exista requisito real.

### 14.5 FamaAgent

Mesmo princípio do Reno.

---

## 15. Mudanças esperadas no repositório Brain

### 15.1 `src/brain/config.py`

Implementar:

- principals dinâmicos;
- `mode = gateway|worker`;
- allowlist server-side de tools;
- `whatsapp_session_dir`;
- credencial gateway;
- validações de paths e digests;
- remoção do hardcode `ALLOWED_PROFILES = {"reno","famaagent"}`.

### 15.2 `src/brain/authorization.py`

Refatorar capability para incluir:

```python
chat_id
```

Separar conceitos:

```text
WorkerRequestIdentity
WorkerCapability
GatewayRequestIdentity
GatewayCapability
AuthorizedConversation
```

A resolução final deve convergir para uma estrutura comum:

```python
AuthorizedConversation(
    principal=...,
    source="whatsapp",
    chat_type="dm",
    chat_id=...,
    session_key=...,
    session_ids=(...)
)
```

### 15.3 `src/brain/whatsapp_identity.py` — novo

Responsável por:

- validar JIDs;
- distinguir `@s.whatsapp.net` de `@lid`;
- ler somente mappings permitidos;
- resolver telefone único;
- falhar fechado;
- não logar identificadores.

### 15.4 `src/brain/service.py`

Adicionar:

- dispatch de `conversation_phone`;
- ACL de tool por principal;
- resultado sanitizado;
- auditoria sem PII;
- suporte aos dois tipos de capability.

`FORBIDDEN_ARGUMENTS` permanece e deve continuar incluindo todos os campos de identidade.

### 15.5 `src/brain/mcp_server.py`

Adicionar o schema e tool:

```text
conversation_phone
```

Manter resources/prompts desnecessários.

### 15.6 Endpoint gateway

Pode ficar em novo módulo, preferencialmente:

```text
src/brain/gateway_api.py
```

ou numa rota claramente separada do adapter MCP.

Não misturar parsing da bridge com lógica do resolvedor.

### 15.7 `scripts/install_brain_secrets.py`

Remover:

```python
PROFILES = ("reno", "famaagent")
```

Criar provisioning para:

- `porteiro`;
- `cadastro`;
- `reno`;
- `famaagent`;
- `default` gateway bridge.

Cada principal usa credencial distinta.

Não imprimir tokens.

### 15.8 `deploy/brain.toml.example`

Atualizar para principals e `whatsapp_session_dir`.

### 15.9 `deploy/hermes-brain.example.yaml`

Adicionar exemplos separados:

- Porteiro;
- Cadastro;
- Reno;
- FamaAgent;
- CEO plugin.

### 15.10 `README.md`

Nova definição:

> Brain é um serviço localhost-only, read-only e capability-scoped de contexto de conversas Hermes.

Documentar histórico + identidade.

### 15.11 `docs/runbook.md`

Adicionar:

- instalação da bridge CEO;
- provisioning dos novos tokens;
- validação do diretório WhatsApp;
- testes LID;
- rollback específico;
- pós-`hermes update`.

---

## 16. Plugin Hermes para o CEO

Adicionar ao projeto Brain uma implementação versionada, por exemplo:

```text
integrations/hermes/brain-ceo-bridge/
├── plugin.yaml
├── __init__.py
├── schemas.py
└── tools.py
```

Deployment instala/copia essa árvore para:

```text
/root/.hermes/plugins/brain-ceo-bridge/
```

### 16.1 Manifest

Deve:

- registrar somente a ferramenta necessária;
- declarar `requires_env` para o token da bridge;
- não solicitar capabilities desnecessárias.

### 16.2 Toolset

```text
brain-context
```

### 16.3 Tool

```text
conversation_phone()
```

### 16.4 Regras do handler

Obrigatório:

- usar `gateway.session_context.get_session_env`;
- nunca usar `os.getenv("HERMES_SESSION_CHAT_ID")` como fonte primária no gateway concorrente;
- exigir WhatsApp DM;
- obter contexto atual dentro da chamada;
- chamar apenas `127.0.0.1`;
- timeout curto;
- nunca aceitar identidade em args;
- nunca logar contexto bruto;
- sanitizar erro técnico.

### 16.5 Concorrência

Teste obrigatório:

Duas mensagens WhatsApp simultâneas, de contatos diferentes, chamando `conversation_phone()` não podem trocar identidade.

Esse é um critério de aceite crítico.

---

## 17. Mudanças na configuração Hermes da Fama

Nenhuma mudança no core `NousResearch/hermes-agent`.

Mudanças somente no repositório/configurações controladas pela Fama.

### 17.1 CEO `/root/.hermes/config.yaml`

Habilitar plugin:

```yaml
plugins:
  enabled:
    - brain-ceo-bridge
```

Adicionar ao WhatsApp:

```yaml
platform_toolsets:
  whatsapp:
    - kanban
    - clarify
    - skills
    - vision
    - brain-context
```

Não configurar Brain MCP diretamente no CEO para esta função.

Motivo: a bridge plugin é quem captura a sessão gateway task-local.

### 17.2 Porteiro

Adicionar:

```yaml
mcp_servers:
  brain:
    url: http://127.0.0.1:8765/mcp
    headers:
      Authorization: Bearer ${BRAIN_TOKEN}
      X-Hermes-Task: ${HERMES_KANBAN_TASK}
      X-Hermes-Run: ${HERMES_KANBAN_RUN_ID}
    tools:
      include:
        - conversation_phone
    resources: false
    prompts: false
```

CLI:

```yaml
platform_toolsets:
  cli:
    - clarify
    - brain
    - famachat
```

Telegram e WhatsApp do Profile devem continuar sem Brain MCP:

```yaml
telegram:
  - clarify
  - no_mcp

whatsapp:
  - clarify
  - no_mcp
```

### 17.3 Cadastro

Mesmo desenho do Porteiro, com FamaChat Cadastro.

### 17.4 Reno/FamaAgent

Preservar configuração existente da V1.

---

## 18. Mudanças de comportamento dos agentes

### 18.1 CEO

A skill/runtime do CEO deve estabelecer:

> Em WhatsApp DM, antes da primeira Task dependente de identidade, chamar `conversation_phone`. Nunca usar display name ou texto como identidade.

### 18.2 Porteiro

Alterar:

> “Sem telefone no cartão, sempre bloqueie”

para:

> “Se o cartão não trouxer telefone, tente `conversation_phone()` do Brain. Somente bloqueie se a capability não estiver disponível ou não resolver um telefone único.”

### 18.3 Cadastro

Mesma alteração.

### 18.4 Não alterar a regra de negócio

Porteiro continua responsável por:

- `sistema_users`;
- `isActive`;
- normalização brasileira.

Cadastro continua responsável por:

- clientes;
- classificação;
- criação;
- normalização brasileira.

Brain fornece somente a identidade de transporte.

---

## 19. Contrato de cartão

Contrato recomendado:

```yaml
schema_version: 1
correlation_id: <uuid>
idempotency_key: <...>

source:
  platform: whatsapp
  chat_id: <interno-ou-redigido-conforme-politica>
  message_id: <id>

contact:
  phone_e164: <telefone-provido-pelo-brain-ou-null>
  display_name: <nome-ou-null>

identity:
  source: brain
  status: resolved | unavailable
  resolution_required_by_worker: true | false

original_message: <texto>
conversation_context: <mínimo>
upstream_result: <...>
request: <trabalho>
expected_output: <contrato>
test_mode: false
```

Se `phone_e164` for `null`, isso não autoriza inferência.

O worker só pode preencher a necessidade operacional via `conversation_phone()` da própria capability.

---

## 20. Health e observabilidade

Expandir `/health` sem PII.

Exemplo:

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

Não incluir:

- paths;
- quantidade de mappings;
- telefones;
- contatos.

### 20.1 Auditoria

Eventos podem registrar:

- timestamp;
- principal;
- mode (`worker`/`gateway`);
- tool;
- allow/deny/unavailable;
- reason code;
- latência.

Nunca contato.

---

## 21. Códigos de erro

Sugestão interna:

```text
AUTH_INVALID_TOKEN
AUTH_MODE_MISMATCH
AUTH_TOOL_DENIED
AUTH_TASK_INVALID
AUTH_RUN_MISMATCH
AUTH_ORIGIN_MISSING
AUTH_ORIGIN_AMBIGUOUS
AUTH_SESSION_MISMATCH
SCOPE_NOT_WHATSAPP_DM
GATEWAY_CONTEXT_INVALID
GATEWAY_SESSION_MISMATCH
PHONE_MAPPING_UNAVAILABLE
PHONE_MAPPING_INVALID
PHONE_IDENTITY_AMBIGUOUS
PHONE_NOT_RESOLVED
HERMES_CONTRACT_INCOMPATIBLE
```

Mensagem pública permanece genérica quando for autorização.

---

## 22. Testes obrigatórios

### 22.1 Resolver de telefone

- phone JID válido → telefone;
- LID com mapping → telefone;
- LID sem mapping → unavailable;
- mapping malformado → unavailable;
- mapping vazio → unavailable;
- dois telefones para um LID → ambiguous;
- caracteres de path traversal → rejeitar;
- grupo → rejeitar;
- JID desconhecido → unavailable;
- `session_key` sozinho nunca prova telefone.

### 22.2 Worker authorization

- Porteiro válido / Task válida → phone;
- Cadastro válido → phone;
- cross-profile → deny;
- cross-task → deny;
- Run antigo → deny;
- Task não running → deny;
- origem Telegram → deny;
- grupo WhatsApp → deny;
- subscription com dois chat IDs → deny.

### 22.3 Gateway/CEO

- CEO WhatsApp DM válida → phone;
- CEO Telegram → deny/unavailable;
- contexto sem `session_id` → deny;
- `session_id` que não combina com `chat_id` → deny;
- `session_key` divergente → deny;
- token worker no endpoint gateway → deny;
- token gateway no endpoint worker → deny.

### 22.4 Concorrência

Teste com duas ContextVars simultâneas:

```text
Contato A → phone A
Contato B → phone B
```

Rodando em paralelo centenas de vezes.

Nunca:

```text
A → phone B
B → phone A
```

### 22.5 Schema MCP

`conversation_phone` não pode expor propriedades de identidade.

### 22.6 Logs

Testes devem capturar logs e provar ausência de:

- phone;
- LID;
- chat IDs;
- session keys;
- tokens.

---

## 23. Smoke test pós-`hermes update`

O gate atual já valida integração Hermes.

Ele deve ser ampliado.

Após cada:

```bash
hermes update
```

executar:

```bash
/root/brain/.venv/bin/python scripts/smoke_test.py
```

O smoke deve verificar:

1. `gateway.session_context.get_session_env` ainda existe;
2. os nomes de contexto necessários ainda são suportados;
3. workers ainda recebem `HERMES_KANBAN_TASK`;
4. workers ainda recebem `HERMES_KANBAN_RUN_ID`;
5. auto-subscription ainda captura origem confiável;
6. sessions WhatsApp ainda possuem `chat_id`, `chat_type`, `session_key`;
7. o diretório configurado de WhatsApp está acessível;
8. o contrato `lid-mapping` esperado ainda coincide com o Hermes instalado;
9. plugin `brain-ceo-bridge` ainda é descoberto/habilitado;
10. toolset `brain-context` aparece apenas onde esperado;
11. Brain MCP aparece nos workers corretos;
12. `no_mcp` continua impedindo exposição em Telegram/WhatsApp dos workers;
13. `/health` retorna compatível;
14. tool schemas não vazam identidade.

Qualquer quebra:

```text
FAIL
↓
não promover update
↓
desabilitar capacidade afetada
↓
corrigir compatibilidade
```

Nunca cair para “usar o número que parece certo”.

---

## 24. Compatibilidade com `hermes update`

A implementação deve obedecer a estas regras:

- zero patch em `/usr/local/lib/hermes-agent`;
- zero arquivo customizado dentro do checkout core;
- Brain vive em `/root/brain`;
- plugin vive em `$HERMES_HOME/plugins/brain-ceo-bridge`;
- configurações ficam no `$HERMES_HOME` da Fama;
- upgrade do Brain é separado;
- upgrade do plugin é separado;
- compatibilidade é testada após upgrade do Hermes.

O plugin de usuário é a extensão oficial do Hermes para tools customizados e não depende de manter um fork do core.

---

## 25. Segurança do diretório WhatsApp

`/root/.hermes/platforms/whatsapp/session` contém credenciais sensíveis da conta WhatsApp.

O Brain não pode tratar esse diretório como fonte genérica de arquivos.

O resolver deve:

- aceitar somente nomes de arquivo que correspondam ao padrão de mapping esperado;
- nunca ler `creds.json` ou arquivos de chaves;
- nunca fornecer listagem desse diretório ao modelo;
- nunca criar endpoint de leitura de arquivo;
- nunca retornar conteúdo bruto;
- operar somente em read-only;
- manter o serviço em localhost.

Embora o processo já rode como root na V1, a superfície de código que toca esse diretório deve ser mínima e isolada em um módulo específico.

---

## 26. Rollout

### Fase 1 — Brain core

Implementar:

- principals;
- ACL;
- `chat_id` na capability;
- resolvedor;
- `conversation_phone`;
- testes unitários.

Ainda sem CEO.

### Fase 2 — Porteiro/Cadastro

- provisionar tokens;
- configurar Brain MCP;
- atualizar regras dos Profiles;
- validar Tasks sintéticas e reais;
- confirmar LID real.

### Fase 3 — CEO bridge

- instalar plugin;
- configurar token gateway;
- habilitar apenas no WhatsApp do CEO;
- validar concorrência;
- validar primeiro cartão com telefone.

### Fase 4 — produção

Executar fluxo completo:

```text
LID real
  ↓
CEO resolve phone
  ↓
Porteiro consulta corretor
  ↓
CEO recebe handoff
  ↓
Cadastro se necessário
  ↓
Reno/FamaAgent conforme roteamento
```

---

## 27. Rollback

Rollback deve ser simples:

1. remover `brain-context` do WhatsApp do CEO;
2. desabilitar `brain-ceo-bridge`;
3. remover Brain dos toolsets CLI de Porteiro/Cadastro;
4. restaurar condutas anteriores de “sem telefone → bloqueio”;
5. manter Brain V1 de Reno/FamaAgent ativo se histórico continuar saudável.

Parar o Brain não altera transcript nem Kanban porque o serviço permanece read-only.

---

## 28. Critérios de aceite

A V2 só está pronta quando todos forem verdadeiros.

### CEO

- [ ] mensagem WhatsApp que chega como LID consegue retornar telefone correto;
- [ ] CEO não recebe LID/chat/session como output;
- [ ] CEO não fornece identidade como argumento;
- [ ] duas conversas concorrentes não se cruzam;
- [ ] Telegram não consegue usar essa capability.

### Porteiro

- [ ] Task vinculada ao mesmo contato resolve o mesmo telefone;
- [ ] telefone ausente no cartão pode ser recuperado pelo Brain;
- [ ] sem mapping, bloqueia em vez de inferir;
- [ ] FamaChat continua sendo a única fonte para “corretor ativo”.

### Cadastro

- [ ] consegue resolver telefone pela própria Task;
- [ ] FamaChat continua sendo a única fonte de cliente/lead;
- [ ] normalização brasileira continua fora do Brain.

### Brain

- [ ] serviço continua read-only;
- [ ] nenhuma identidade é model-selectable;
- [ ] ACL server-side funciona;
- [ ] logs não contêm PII;
- [ ] `conversation_recent` e `conversation_search` não regrediram;
- [ ] post-update smoke passa.

### Hermes

- [ ] nenhuma alteração no core;
- [ ] plugin de usuário permanece após `hermes update`;
- [ ] configuração do CEO só expõe a bridge no WhatsApp;
- [ ] workers só veem tools de Brain necessárias ao papel.

---

## 29. Arquivos estimados a criar ou alterar

### Brain

```text
PRD.md
README.md
src/brain/config.py
src/brain/authorization.py
src/brain/service.py
src/brain/mcp_server.py
src/brain/whatsapp_identity.py             # novo
src/brain/gateway_api.py                   # novo, nome ajustável
tests/test_brain.py
tests/test_whatsapp_identity.py            # recomendado
tests/test_gateway_identity.py             # recomendado
scripts/install_brain_secrets.py
scripts/hermes_integration_check.py
scripts/smoke_test.py
deploy/brain.toml.example
deploy/hermes-brain.example.yaml
docs/runbook.md
docs/conversation-identity-invariant.md    # novo
integrations/hermes/brain-ceo-bridge/...   # novo
```

### Configuração Fama/Hermes

```text
config.yaml
profiles/porteiro/config.yaml
profiles/porteiro/SOUL.md
profiles/cadastro/config.yaml
profiles/cadastro/SOUL.md
profiles/reno/config.yaml                  # somente se houver mudança de allowlist
profiles/famaagent/config.yaml             # somente se houver mudança de allowlist
skill/runtime do CEO
```

---

## 30. Sequência recomendada de implementação

1. Refatorar credenciais/ACL sem alterar comportamento V1.
2. Adicionar `chat_id` à capability worker.
3. Implementar resolvedor WhatsApp isolado.
4. Criar `conversation_phone` MCP.
5. Cobrir testes negativos e concorrência.
6. Integrar Porteiro.
7. Integrar Cadastro.
8. Criar endpoint gateway privado.
9. Criar plugin CEO.
10. Atualizar CEO runtime.
11. Expandir `/health`.
12. Expandir integration/smoke tests.
13. Testar com LID real.
14. Executar fluxo ponta a ponta.
15. Só então promover para produção.

---

## 31. Referências verificadas para esta revisão

### Hermes Agent — documentação

- https://hermes-agent.nousresearch.com/docs/user-guide/sessions
- https://hermes-agent.nousresearch.com/docs/user-guide/messaging/whatsapp
- https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
- https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference
- https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban
- https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban-worker-lanes
- https://hermes-agent.nousresearch.com/docs/getting-started/updating

### Hermes Agent — código upstream

- `gateway/session_context.py`
- `gateway/whatsapp_identity.py`
- `scripts/whatsapp-bridge/bridge.js`
- `tools/kanban_tools.py`
- `hermes_cli/kanban_db.py`
- `tools/mcp_tool.py` / configuração MCP atual

### Brain — código atual

- `README.md`
- `PRD.md` V1.1
- `src/brain/config.py`
- `src/brain/authorization.py`
- `src/brain/service.py`
- `src/brain/mcp_server.py`
- `src/brain/db.py`
- `src/brain/projection.py`
- `scripts/install_brain_secrets.py`
- `scripts/hermes_integration_check.py`
- `scripts/smoke_test.py`
- `docs/runbook.md`
- `deploy/hermes-brain.example.yaml`

---

## 32. Definição final

Após esta implementação, o fluxo de identidade deverá funcionar assim:

```text
Pessoa manda mensagem
        ↓
WhatsApp entrega phone JID ou LID
        ↓
Hermes conhece a conversa
        ↓
Brain recebe somente a conversa autorizada
        ↓
Brain prova LID ↔ telefone
        ↓
conversation_phone()
        ↓
telefone confirmado
        ↓
CEO / Porteiro / Cadastro usam o dado
        ↓
FamaChat decide corretor / cliente / lead
```

A propriedade de segurança mais importante permanece:

> **Nenhum agente escolhe de quem quer descobrir o telefone. O runtime determina a conversa; o Brain somente revela o telefone comprovado daquela conversa.**
