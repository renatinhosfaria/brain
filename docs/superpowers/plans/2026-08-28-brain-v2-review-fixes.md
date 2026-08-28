# Brain V2 Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Corrigir os bloqueadores de identidade e fechar os gaps de contrato, deployment, testes e documentação encontrados na revisão do Brain V2.

**Architecture:** O Brain continua como autoridade única. O resolver constrói evidência semântica somente para mappings relevantes e a autorização usa essa mesma prova para equivalência longitudinal entre aliases. O plugin CEO permanece fino; os scripts passam a verificar a configuração efetiva, as credenciais e o plugin instalado.

**Tech Stack:** Python 3.11+, SQLite read-only, Starlette/MCP, Hermes user plugin, unittest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-28-brain-v2-design.md` e `PRD.md` V2.

## Global Constraints

- Alterar somente arquivos sob `/root/brain`.
- Nunca modificar `/usr/local/lib/hermes-agent`.
- Não usar `session_key`, display name, texto, argumento do modelo ou nome de arquivo isolado como prova suficiente de telefone.
- A ferramenta `conversation_phone` permanece sem argumentos.
- O resultado público de falha de telefone permanece `{"status":"unavailable","reason":"phone_not_resolved"}`.
- Nenhum log ou resposta pública pode conter telefone, LID, `chat_id`, `session_key`, token ou caminho de arquivo.
- Cada correção de código deve ter teste RED antes do código de produção.

---

### Task 1: Isolar falhas de mappings e limitar leituras

**Files:**
- Modify: `src/brain/whatsapp_identity.py`
- Test: `tests/test_whatsapp_identity.py`

**Interfaces:**
- Preserve `resolve_phone(chat_id: str, mapping_dir: Path) -> PhoneResolution`.
- Add a private semantic mapping loader that can distinguish a requested reverse filename from unrelated candidates.

- [ ] **Step 1: Write failing regression tests**

Add tests proving a valid requested mapping still resolves when unrelated candidates are empty or malformed, and that a malformed requested reverse mapping remains unavailable. Add an oversized mapping case that is unavailable without reading an unbounded payload.

- [ ] **Step 2: Run the focused tests and verify RED**

Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_whatsapp_identity -v`.
Expected: the unrelated-invalid and size cases fail against the current global-fail implementation.

- [ ] **Step 3: Implement the minimal resolver fix**

Keep the configured directory and exact filename regexes. Parse valid forward files individually, skip malformed candidates whose filename cannot identify the requested LID, and treat a malformed/symlink/non-regular requested reverse file as invalid. Reject files above a fixed small byte limit before `read_text`. Keep valid observations grouped by LID and return ambiguity only when valid evidence yields different phones.

- [ ] **Step 4: Run focused and full tests**

Run the focused resolver tests, then `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`.
Expected: all resolver and existing tests pass.

- [ ] **Step 5: Commit**

Commit with `fix: isolate invalid WhatsApp mappings by identity`.

### Task 2: Provar equivalência semântica de aliases na autorização

**Files:**
- Modify: `src/brain/whatsapp_identity.py`
- Modify: `src/brain/authorization.py`
- Modify: `src/brain/service.py`
- Test: `tests/test_brain.py`

**Interfaces:**
- Add a private helper accepting the current `chat_id`, longitudinal candidate IDs and mapping directory, returning whether all IDs are the same exact identifier or resolve to one verified phone.
- Worker and gateway authorization must use the same helper before returning `session_ids`.

- [ ] **Step 1: Write failing tests**

Add one worker test where a different LID shares the same `session_key` but has no mapping and must be denied, and one test where a phone JID and a mapped LID for the same phone remain allowed. Add a gateway test for the same valid alias transition.

- [ ] **Step 2: Run focused tests and verify RED**

Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_brain tests.test_gateway_api -v`.
Expected: the unverified worker alias currently passes and the valid gateway alias currently fails.

- [ ] **Step 3: Implement shared equivalence validation**

Fetch longitudinal rows with `chat_id`. Exact equality is allowed; differing IDs require `resolve_phone` to return `ok` for each and the same phone. Any unavailable, ambiguous or conflicting result raises the existing fail-closed authorization error. Keep the service’s phone tool resolving only the authorized current chat.

- [ ] **Step 4: Run focused and full tests**

Run both focused tests and the complete suite.

- [ ] **Step 5: Commit**

Commit with `fix: require verified WhatsApp alias equivalence`.

### Task 3: Fechar contratos de argumentos e limites de payload

**Files:**
- Modify: `src/brain/service.py`
- Modify: `src/brain/gateway_api.py`
- Modify: `integrations/hermes/brain-ceo-bridge/tools.py`
- Test: `tests/test_brain.py`
- Test: `tests/test_gateway_api.py`
- Test: `tests/test_ceo_bridge_plugin.py`

**Interfaces:**
- `conversation_phone` must accept exactly `{}` through the Brain service.
- Gateway request body remains capped at 16 KiB and plugin HTTP response is capped before JSON parsing.

- [ ] **Step 1: Write failing tests**

Add a direct service test with an arbitrary argument, a gateway oversized-body test, and a plugin oversized-response test. Each must return the existing sanitized failure contract.

- [ ] **Step 2: Run focused tests and verify RED**

Run the three focused test modules and confirm the current implementation accepts or buffers the invalid input.

- [ ] **Step 3: Implement minimal validation and bounds**

Require an empty mapping for `conversation_phone`, add identity/path keys to defense-in-depth forbidden arguments, reject oversized bodies using content length and bounded reads, and reject oversized plugin responses before parsing.

- [ ] **Step 4: Run focused and full tests**

Run the focused modules and the complete suite.

- [ ] **Step 5: Commit**

Commit with `fix: enforce Brain phone boundary contracts`.

### Task 4: Completar gates de deployment, secrets e CEO

**Files:**
- Modify: `scripts/hermes_integration_check.py`
- Modify: `scripts/smoke_test.py`
- Modify: `scripts/install_brain_secrets.py`
- Create: `deploy/hermes-ceo-brain.example.yaml`
- Modify: `tests/test_deployment_contracts.py`

**Interfaces:**
- The integration check must require the CEO config, validate all five digest/token pairs, inspect the installed plugin, and resolve `brain-context` per platform.
- The CEO example must enable `brain-ceo-bridge` and expose `brain-context` only on WhatsApp.

- [ ] **Step 1: Write failing contract tests**

Add tests asserting the CEO example contains the plugin and platform scope, the checker references all required context fields and gateway token, and the smoke invokes the gateway compatibility path and mapping contract.

- [ ] **Step 2: Run focused tests and verify RED**

Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_deployment_contracts -v` and confirm the missing contract assertions fail.

- [ ] **Step 3: Implement deployment gate changes**

Require existing CEO/server config at runtime, validate the gateway secret digest and all five uniqueness checks, use Hermes’ effective resolver for `brain-context` on CEO platforms, validate actual installed plugin discovery/registration, and add a non-PII gateway auth/invalid-body probe plus mapping compatibility probe to smoke. Keep raw secrets out of output. Document safe preflight/rollback behavior for secret rotation.

- [ ] **Step 4: Run focused and script checks**

Run deployment tests, both script `--help` commands, full tests and Ruff.

- [ ] **Step 5: Commit**

Commit with `fix: close Hermes V2 deployment gates`.

### Task 5: Atualizar documentação, metadados e higiene do patch

**Files:**
- Modify: `README.md`
- Modify: `docs/runbook.md`
- Modify: `deploy/brain.service`
- Modify: `pyproject.toml`
- Modify: `PRD.md`
- Modify: `docs/superpowers/specs/2026-08-28-brain-v2-design.md`
- Test: `tests/test_deployment_contracts.py`

- [ ] **Step 1: Write failing documentation contract tests**

Add assertions for CEO toolset configuration, complete rollback coverage, current V2 status and updated service/package descriptions.

- [ ] **Step 2: Run focused tests and verify RED**

Run the deployment contract tests and confirm the current documentation misses at least one required statement.

- [ ] **Step 3: Update docs and formatting**

Document CEO WhatsApp-only setup, disable all V2 surfaces during rollback, update V2 descriptions/status, and remove trailing Markdown hard-break spaces. Format changed Python files with Ruff.

- [ ] **Step 4: Run final verification**

Run:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check src tests scripts integrations
.venv/bin/ruff format --check src tests scripts integrations
git diff --check
git status --short
```

- [ ] **Step 5: Commit**

Commit with `docs: complete Brain V2 review remediation`.
