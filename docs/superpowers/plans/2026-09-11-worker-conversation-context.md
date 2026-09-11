# Worker Conversation Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expor `conversation_context` aos profiles Porteiro, Cadastro e Reno com a mesma resolução autenticada e o mesmo payload já usado pelo CEO.

**Architecture:** Reutilizar a projeção de contexto do Brain atrás de uma capability de worker autorizada por Task/Run. Registrar a ferramenta na superfície MCP worker e habilitá-la apenas nas três allowlists, mantendo Kanban como barramento de objetivo e resultados.

**Tech Stack:** Python 3.11+, unittest, MCP Streamable HTTP, configuração TOML/YAML Hermes.

**Spec:** `docs/superpowers/specs/2026-09-11-worker-conversation-context-design.md`

## Global Constraints

- Identidade deriva somente de contexto Hermes autenticado; argumentos livres de identidade são proibidos.
- Somente WhatsApp DM autorizado está em escopo.
- CTWA raw é evidência não confiável e não entra em logs, summary ou metadata.
- Brain lê `state.db` e `kanban.db` em modo somente leitura.
- Profiles fora de Porteiro, Cadastro e Reno permanecem sem a ferramenta.

### Task 1: Testar a capability de worker e a projeção comum

**Files:**
- Modify: `tests/test_service.py`, `tests/test_authorization.py`
- Modify: `src/brain/service.py`, `src/brain/authorization.py`

- [ ] Escrever testes falhando para uma task worker running autorizada obter o mesmo payload CTWA da rota gateway, e para task/run/assignee/origem inválidos falharem.
- [ ] Executar os testes e confirmar falha por capability ausente.
- [ ] Extrair a resolução autorizada de conversa para uma capability compartilhada, preservando limites, validação e auditoria existentes.
- [ ] Implementar `conversation_context` no dispatcher worker sem argumentos e retornar o contrato existente.
- [ ] Executar os testes unitários específicos e a suíte Brain.

### Task 2: Registrar e restringir a ferramenta MCP

**Files:**
- Modify: `src/brain/mcp_server.py`, `src/brain/config.py`
- Modify: `deploy/hermes-brain.example.yaml`, `deploy/hermes-brain-memory.example.yaml`
- Modify: configurações de Porteiro, Cadastro e Reno em `/root/.hermes`

- [ ] Adicionar teste de listagem MCP e allowlist verificando presença somente nos três profiles.
- [ ] Executar o teste para confirmar falha antes da configuração.
- [ ] Registrar o schema zero-argumento e habilitar a capability nas três configurações.
- [ ] Manter CEO bridge funcional e negar outros profiles.
- [ ] Executar testes MCP e `smoke_test.py`.

### Task 3: Atualizar instruções operacionais dos profiles

**Files:**
- Modify: skills/SOUL de Porteiro, Cadastro e Reno relevantes em `/root/.hermes`
- Modify: `/root/.hermes/ops/hermes-team/RUNBOOK.md`

- [ ] Adicionar instrução para consultar `conversation_context({})` diretamente quando a task exigir CTWA ou contexto recente.
- [ ] Exigir tratamento do retorno como evidência e proibir argumentos de identidade.
- [ ] Definir que CEO continua fornecendo objetivo e resultados via Kanban.
- [ ] Documentar a exposição do mesmo payload, inclusive raw CTWA, e os limites de retenção.
- [ ] Rodar `verify_team.py core` e a suíte Hermes correspondente.

### Task 4: Validação integrada e revisão

**Files:**
- Modify: `tests/test_mcp_server.py` ou equivalente existente
- Modify: `ops/hermes-team/verify_team.py` se necessário

- [ ] Adicionar fixture sintética com cinco eventos CTWA e validar campos completos e igualdade CEO/worker.
- [ ] Testar indisponibilidade, DM ambígua, Telegram e ferramenta em profile não autorizado.
- [ ] Rodar Ruff, unittest completo Brain, smoke de compatibilidade e verificadores Hermes.
- [ ] Revisar diffs dos dois repositórios e registrar instalação/ativação pendente.
