# OAuth dinâmico do Brain para Meta Ads MCP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Substituir o cliente OAuth pré-registrado do Brain por registro dinâmico DCR, com login automático, sem App ID/App Secret fornecidos pelo operador.

**Architecture:** O cliente fará discovery dos metadados do Meta Ads MCP, validará o `registration_endpoint` e registrará um cliente público com callback loopback e `ads_read`. A registration e os tokens serão persistidos juntos em envelope AES-256-GCM root-only; o serviço continuará usando o mesmo provedor lazy, refresh automático, circuit breaker, atribuição CTWA e fallback explícito por token.

**Tech Stack:** Python 3, `urllib`, `cryptography` AES-GCM, `anyio`, `httpx2`, MCP SDK, `unittest`, Ruff, CLI Python e systemd.

**Spec:** `docs/superpowers/specs/2026-09-02-ctwa-meta-ads-oauth-dcr-design.md`

## Global Constraints

- O resource fixo é `https://mcp.facebook.com/ads` e a conta é `act_1598606388477916`.
- O callback fixo é `http://127.0.0.1:8766/oauth/callback`; acesso externo ocorre somente por túnel SSH.
- DCR solicita somente `ads_read`, `token_endpoint_auth_method=none`, `authorization_code` e `refresh_token`.
- Scopes concedidos devem ser exatamente os solicitados; `ads_management`, `business_management` e qualquer escrita são rejeitados.
- Envelopes do modelo antigo com App ID/App Secret não são migrados; devem ser removidos com `clear` antes do primeiro login DCR.
- O modo `token` permanece rollback explícito; não existe fallback silencioso.
- Nenhum token, código, segredo, registration access token, resposta Meta ou identificador bruto aparece em logs, SQLite, `conversation_context({})`, nomes de arquivo ou erros.
- Testes de registro usam somente servidores HTTP locais falsos; nenhum POST real na Meta será feito durante a implementação.

---

### Task 1: Modelar metadata e cliente DCR

**Files:**
- Modify: `src/brain/meta_ads_oauth.py: OAuthMetadata, metadata validation`
- Test: `tests/test_meta_ads_oauth.py`

**Interfaces:**
- Consumes: metadata JSON do authorization server.
- Produces: `OAuthMetadata.registration_endpoint: str`; `OAuthDynamicClient` com `client_id`, segredo opcional, registration access token opcional, expiração, issuer/resource/callback e scopes; validação bounded para todos os campos.

- [ ] **Step 1: Write the failing tests**

Adicionar testes que façam discovery aceitar `registration_endpoint` somente em `https://mcp.facebook.com`, rejeitem HTTP, credenciais, fragmento, portas não padrão e hosts externos, e validem um `OAuthDynamicClient` com scopes somente `ads_read`. Adicionar teste que metadata sem registration endpoint produza `oauth_metadata_invalid` no modo DCR.

- [ ] **Step 2: Run the tests to verify they fail**

~~~bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_oauth.MetaAdsOAuthTests.test_discover_validates_registration_endpoint tests.test_meta_ads_oauth.MetaAdsOAuthTests.test_dynamic_client_validation
~~~

Esperado: falha porque `OAuthMetadata` não expõe o endpoint de registro nem existe `OAuthDynamicClient`.

- [ ] **Step 3: Implement the minimal model and validation**

Estender `_parse_metadata` para validar e guardar o endpoint. Criar dataclass imutável `OAuthDynamicClient` com validação de textos bounded, resource/issuer fixos, callback exato e scopes exatamente `frozenset({"ads_read"})`.

- [ ] **Step 4: Run the focused tests and existing OAuth tests**

~~~bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_oauth
~~~

Esperado: PASS, preservando PKCE, allowlist, store, callback e refresh existentes.

- [ ] **Step 5: Commit**

~~~bash
git add src/brain/meta_ads_oauth.py tests/test_meta_ads_oauth.py
git commit -m "feat: model Meta Ads dynamic OAuth clients"
~~~

### Task 2: Implementar registro dinâmico seguro

**Files:**
- Modify: `src/brain/meta_ads_oauth.py: MetaAdsOAuth discovery/authorization`
- Test: `tests/test_meta_ads_oauth.py`

**Interfaces:**
- Consumes: `OAuthMetadata` e callback configurado.
- Produces: `MetaAdsOAuth.register_dynamic_client() -> OAuthDynamicClient` e `_post_json()` sem redirects, com timeout e limite de resposta.

- [ ] **Step 1: Write the failing tests**

Adicionar um servidor HTTP local falso que verifique POST JSON no endpoint DCR, body com `redirect_uris`, `grant_types`, `response_types`, `token_endpoint_auth_method=none`, `client_name` bounded e scope `ads_read`. Testar retorno de `client_id`, reuso dos campos permitidos, rejeição de resposta sem `client_id`, scope extra, callback divergente, método de auth diferente de `none`, redirect HTTP e resposta acima do limite.

- [ ] **Step 2: Run the tests to verify they fail**

~~~bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_oauth.MetaAdsOAuthTests.test_register_dynamic_client_posts_minimal_payload tests.test_meta_ads_oauth.MetaAdsOAuthTests.test_register_dynamic_client_rejects_overgrant
~~~

Esperado: falha porque não há método DCR nem transporte JSON.

- [ ] **Step 3: Implement the bounded DCR request**

Criar `_post_json` usando o opener sem redirects e allowlist de hosts. Fazer `register_dynamic_client()` enviar apenas os campos do contrato, validar a resposta estritamente, aceitar segredo/token de registro somente dentro dos limites e não incluir texto remoto em `OAuthError`.

- [ ] **Step 4: Integrate registration into authorization URL**

Alterar `authorization_url()` para chamar `ensure_dynamic_client()` quando não houver registration válida e usar o `client_id` dinâmico. Manter state, PKCE, resource e `ads_read` exatos. Não adicionar scopes opcionais com base apenas em `scopes_supported`.

- [ ] **Step 5: Run focused tests**

~~~bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_oauth
~~~

Esperado: PASS, incluindo testes de redirect, PKCE, scopes exatos e troca de code.

- [ ] **Step 6: Commit**

~~~bash
git add src/brain/meta_ads_oauth.py tests/test_meta_ads_oauth.py
git commit -m "feat: register Meta Ads OAuth clients dynamically"
~~~

### Task 3: Substituir o envelope pré-registrado pelo envelope DCR

**Files:**
- Modify: `src/brain/meta_ads_oauth.py: encrypted payload load/save`
- Modify: `src/brain/meta_attribution.py: credential provider construction if needed`
- Test: `tests/test_meta_ads_oauth.py`, `tests/test_meta_attribution.py`

**Interfaces:**
- Consumes: `OAuthDynamicClient` e `OAuthCredentials`.
- Produces: payload v2 estrito com `dynamic_client` e `credentials`; carga `from_store_or_new()`; `clear_store()` sem decrypt; provider lazy com refresh, reload atômico e invalidação após `invalid_grant`.

- [ ] **Step 1: Write the failing tests**

Adicionar testes que persistam registration sem tokens, recarreguem a mesma registration após reinício, persistam registration + credentials no mesmo envelope e rejeitem payloads antigos de `client_configuration` com erro `oauth_legacy_store`. Testar que `clear_store()` remove envelope válido e corrompido sem tentar descriptografar e que `invalid_grant` substitui o envelope por uma registration sem tokens.

- [ ] **Step 2: Run the tests to verify they fail**

~~~bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_oauth.MetaAdsOAuthTests.test_legacy_pre_registered_store_is_rejected tests.test_meta_ads_oauth.MetaAdsOAuthTests.test_dynamic_registration_survives_restart
~~~

Esperado: falha porque o store atual aceita `client_configuration` e não conhece payload v2.

- [ ] **Step 3: Implement strict v2 storage**

Criar mapeadores estritos para `dynamic_client` e `credentials`, preservar AES-GCM/AAD/chave/permissões/escrita atômica, e fazer `from_store_or_new()` instanciar um cliente sem ID quando o arquivo não existir. Envelopes antigos não serão migrados; `clear` continua operando somente sobre o caminho privado.

- [ ] **Step 4: Rewire provider and invalidation**

Fazer o provider carregar a registration e credentials separadamente, usar o client ID dinâmico em refresh, manter marker/fingerprint e, após `invalid_grant`, remover tokens de forma atômica preservando apenas registration válida.

- [ ] **Step 5: Run focused integration tests**

~~~bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_oauth tests.test_meta_attribution
~~~

Esperado: PASS, sem token em `repr`, logs, estado de saúde ou contexto.

- [ ] **Step 6: Commit**

~~~bash
git add src/brain/meta_ads_oauth.py src/brain/meta_attribution.py tests/test_meta_ads_oauth.py tests/test_meta_attribution.py
git commit -m "feat: persist dynamic OAuth registration securely"
~~~

### Task 4: Atualizar CLI, serviço e configuração

**Files:**
- Modify: `scripts/meta_ads_oauth.py`
- Modify: `src/brain/service.py`, `src/brain/config.py`
- Modify: `tests/test_deployment_contracts.py`, `tests/test_config.py`
- Modify: `docs/runbook.md`, `README.md`, `deploy/brain.env.example`, `deploy/brain.toml.example`

**Interfaces:**
- Consumes: `MetaAdsOAuth.from_store_or_new()`, `ensure_dynamic_client()` e `OAuthCredentialProvider`.
- Produces: `login` como único fluxo de configuração; `status`, `clear` e `probe` sem App ID/App Secret; serviço OAuth fail-open e token rollback.

- [ ] **Step 1: Write the failing tests**

Testar que o parser não oferece `configure`, `login` não pede App ID/Secret, `status` retorna `missing` sem store, `clear` funciona em store legado/corrupto, `probe` usa a conta fixada e serviço OAuth inicializa o provider DCR sem ler variáveis de App ID/Secret. Atualizar contratos de deploy para remover instruções de configuração pré-registrada e exigir o comando `clear` no rollout.

- [ ] **Step 2: Run the tests to verify they fail**

~~~bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_deployment_contracts tests.test_config
~~~

Esperado: falha porque a CLI ainda expõe `configure` e o serviço espera um client ID vindo do store.

- [ ] **Step 3: Implement the CLI transition**

Remover `configure` e prompts de secret. Fazer `login` descobrir/registrar automaticamente, mostrar somente a URL e mensagens bounded, manter o túnel SSH, `status`, `clear` e `probe`, e rejeitar explicitamente envelope legado com mensagem técnica curta.

- [ ] **Step 4: Wire service and config**

No modo `oauth`, o serviço deverá criar `MetaAdsOAuth.from_store_or_new()` e o provider DCR; em modo `token`, manter exatamente o provider legado. Nenhuma falha OAuth poderá interromper atendimento ou produzir fallback implícito.

- [ ] **Step 5: Update operational documentation and deployment contract**

Documentar `clear` do envelope antigo, `login` automático, callback/túnel, `status`, `probe`, escopo `ads_read`, rollout, monitoramento e rollback. Remover referências a App ID/App Secret e à migração automática.

- [ ] **Step 6: Run focused tests and commit**

~~~bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_meta_ads_oauth tests.test_meta_ads_mcp tests.test_config tests.test_meta_attribution tests.test_deployment_contracts
git add scripts/meta_ads_oauth.py src/brain/service.py src/brain/config.py tests/test_deployment_contracts.py tests/test_config.py docs/runbook.md README.md deploy/brain.env.example deploy/brain.toml.example
git commit -m "feat: switch Brain OAuth login to dynamic registration"
~~~

### Task 5: Verificação completa e rollout seguro

**Files:**
- Test: toda a suíte `tests/` e `observers/whatsapp/test/`
- Modify: nenhum arquivo de produção salvo que um teste revele regressão

**Interfaces:**
- Consumes: branch DCR completo e todos os contratos existentes.
- Produces: evidência de testes, revisão de segurança e checklist operacional para o primeiro login real.

- [ ] **Step 1: Run all Python tests**

~~~bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
~~~

Esperado: toda a suíte verde.

- [ ] **Step 2: Run Observer tests**

~~~bash
cd observers/whatsapp
npm install
npm test
~~~

Esperado: toda a suíte Node verde, sem chamadas à Meta ou ao Brain de produção.

- [ ] **Step 3: Run static and diff checks**

~~~bash
.venv/bin/ruff check src/brain tests scripts
.venv/bin/ruff format --check src/brain/config.py src/brain/meta_ads_mcp.py src/brain/meta_attribution.py src/brain/meta_ads_oauth.py src/brain/service.py scripts/meta_ads_oauth.py tests/test_config.py tests/test_deployment_contracts.py tests/test_meta_ads_mcp.py tests/test_meta_ads_oauth.py
git diff --check
~~~

- [ ] **Step 4: Perform final security review**

Revisar o diff contra a spec, confirmar que nenhum App Secret/token/código pode aparecer em logs ou contexto, confirmar que endpoints DCR/token não seguem redirects e confirmar que envelopes antigos são rejeitados até `clear`.

- [ ] **Step 5: Commit verification evidence**

~~~bash
git status --short
git log --oneline -5
~~~

Não adicionar saídas de testes ou tokens ao repositório. Registrar somente a evidência no handoff operacional.

- [ ] **Step 6: Execute rollout only with real operator consent**

No host do Brain, manter atribuição desabilitada, executar `clear` para o envelope antigo, rodar `login` via túnel SSH, depois `status` e `probe`. Só após o probe passar habilitar `BRAIN_META_ADS_MCP_AUTH_MODE=oauth`, reiniciar o serviço e fazer um CTWA controlado. Revogar a app/consentimento antigo apenas depois da confirmação do novo fluxo.
