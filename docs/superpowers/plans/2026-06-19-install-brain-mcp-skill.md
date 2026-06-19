# Install Brain MCP Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Criar uma skill repo-scoped que ensina agentes a conectar o MCP do `brain` em Codex, Claude e Hermes com seguranca.

**Architecture:** A skill fica em `.agents/skills/install-brain-mcp/SKILL.md` e concentra todo o fluxo em um unico arquivo, sem referencias, scripts ou assets. O conteudo combina fatos do `brain`, um fluxo comum de instalacao, matriz por cliente, verificacao e troubleshooting.

**Tech Stack:** Codex Skills, Markdown, YAML frontmatter, Model Context Protocol, HTTP streamable MCP, bearer-token authentication, Git.

---

## Source Spec

- `docs/superpowers/specs/2026-06-19-install-brain-mcp-skill-design.md`

Leia o spec antes de executar. A skill deve documentar o comportamento atual do `brain` e os limites conhecidos dos clientes, sem prometer suporte que dependa de OAuth, plugin ou configuracao real fora deste repositorio.

## File Structure

- Create: `.agents/skills/install-brain-mcp/SKILL.md`
  Skill repo-scoped com frontmatter valido e instrucoes completas para instalar, conectar, validar e diagnosticar o MCP do `brain`.

- Remove after scaffold: `.agents/skills/install-brain-mcp/agents/openai.yaml`
  O inicializador de skills cria metadados de UI automaticamente. Remova esse arquivo para preservar o spec aprovado, que exige `SKILL.md` como unico arquivo da skill.

## Task 1: Create The Repo-Scoped Skill

**Files:**
- Create: `.agents/skills/install-brain-mcp/SKILL.md`
- Remove if generated: `.agents/skills/install-brain-mcp/agents/openai.yaml`

- [ ] **Step 1: Confirm the starting tree**

Run:

```bash
git status --short
test ! -e .agents/skills/install-brain-mcp
```

Expected:

- `git status --short` may show the pre-existing unrelated ` D README.md`.
- The `test` command exits with status 0.
- If `.agents/skills/install-brain-mcp` already exists, inspect it before continuing and preserve any user-owned changes.

- [ ] **Step 2: Initialize the skill directory**

Run:

```bash
python /root/.codex/skills/.system/skill-creator/scripts/init_skill.py install-brain-mcp --path .agents/skills --interface display_name="Install Brain MCP" --interface short_description="Connect Brain MCP to AI clients" --interface default_prompt="Install the Brain MCP server for the selected AI client."
```

Expected:

- Command exits with status 0.
- `.agents/skills/install-brain-mcp/SKILL.md` exists.
- `.agents/skills/install-brain-mcp/agents/openai.yaml` may exist because the initializer creates UI metadata.

- [ ] **Step 3: Remove generated UI metadata**

Run:

```bash
rm -rf .agents/skills/install-brain-mcp/agents
find .agents/skills/install-brain-mcp -maxdepth 2 -type f | sort
```

Expected output:

```text
.agents/skills/install-brain-mcp/SKILL.md
```

- [ ] **Step 4: Replace `SKILL.md` with the final skill content**

Replace the entire content of `.agents/skills/install-brain-mcp/SKILL.md` with:

`````markdown
---
name: install-brain-mcp
description: Install, connect, validate, or troubleshoot the Brain MCP server for AI clients and agents, including Codex CLI, Codex Desktop/App, Claude Desktop, Claude Web, Claude Code, and Hermes. Use when configuring Brain's `/mcp` endpoint, bearer-token authentication, client-vs-curator principals, or client-specific MCP setup.
---

# Install Brain MCP

Use this skill when a user wants to connect an AI client or agent to the `brain` MCP server. Treat this as an integration task with live credentials: verify the target client, confirm the server URL, choose the right principal, and avoid exposing bearer tokens.

## Required Inputs

Determine these values before writing or changing client configuration:

- **Target client:** Codex CLI, Codex Desktop/App, Claude Desktop, Claude Web, Claude Code, Hermes, or another MCP-capable client.
- **Brain base URL:** the deployment root, such as `https://brain.example.com`.
- **Brain MCP URL:** the final MCP endpoint, always `<brain-base-url>/mcp`.
- **Principal type:** `curator` for trusted administrative integrations, or `client` for ordinary agent clients.
- **Bearer token source:** an environment variable, secure store, or one-time user-provided token value.
- **Network location:** local machine, private network, public internet, or allowlisted public endpoint.
- **Configuration scope:** user-local, project-local, organization-managed, or Hermes runtime configuration.

Ask for missing values when they cannot be inferred from local files or current environment. Never ask the user to paste a secret if a local environment variable or secure store is already available.

## Brain MCP Facts

Use these project-specific facts:

- The MCP endpoint is `/mcp`.
- The transport is streamable HTTP through FastMCP.
- MCP requests authenticate with `Authorization: Bearer <token>`.
- `GET /health` is public and checks basic service availability.
- `GET /status` uses `BRAIN_AUTH_TOKEN`, but `BRAIN_AUTH_TOKEN` does not authenticate MCP.
- `BRAIN_CURATOR_TOKEN` authenticates the `curator` principal.
- Tokens that start with `brain_client_` authenticate agent clients created by Brain curator tools.
- Clients can search and read curated notes and may submit agent notes when their permissions allow it.
- Curator credentials can administer clients, raw agent notes, curated notes, graph maintenance, and other protected MCP tools.
- `_agents/` is raw inbox workflow state. Ordinary clients must not treat it as public searchable content.

## Common Setup Flow

Follow this sequence for every client:

1. Identify the target client and its current MCP configuration mechanism.
2. Normalize the URL:
   - If the user gives `https://brain.example.com`, use `https://brain.example.com/mcp`.
   - If the user gives `https://brain.example.com/mcp`, keep it.
   - Do not use `/health` or `/status` as the MCP URL.
3. Verify service availability when network tools are available:

   ```bash
   curl -fsS "$BRAIN_BASE_URL/health"
   ```

4. Select the token:
   - Use a `brain_client_` token for ordinary agent clients.
   - Use `BRAIN_CURATOR_TOKEN` only for Hermes or another trusted administrative integration.
   - Do not use `BRAIN_AUTH_TOKEN` for MCP.
5. Prefer environment-variable based token configuration when the client supports it.
6. Write only configuration needed by the selected client.
7. Reload or restart the client if its MCP configuration is loaded at startup.
8. Verify the MCP server appears in the client and exposes expected tools.
9. Diagnose failures by layer: network, URL, transport, authentication, principal permissions, then client-specific loading behavior.

## Client Matrix

### Codex CLI

Codex supports streamable HTTP MCP servers through `config.toml`. Prefer this shape for user-level configuration:

```toml
[mcp_servers.brain]
url = "https://brain.example.com/mcp"
bearer_token_env_var = "BRAIN_MCP_TOKEN"
```

For project-scoped configuration in a trusted repository, use `.codex/config.toml` with the same table. For user-wide configuration, use the user's Codex config file.

When executing setup:

```bash
export BRAIN_MCP_TOKEN="<BRAIN_MCP_TOKEN>"
codex mcp --help
```

Use `codex mcp --help` to confirm the installed CLI's exact add/update flags before using command-line MCP management. If flags are unclear or differ from the current documentation, edit `config.toml` directly instead of guessing.

Verify inside the Codex TUI with:

```text
/mcp
```

Expected result: a server named `brain` appears. If it does not, check that the config file is in the active Codex scope and that the shell launching Codex has `BRAIN_MCP_TOKEN` set.

### Codex Desktop/App

Codex skills are available in the Codex app, but direct MCP configuration support can differ by surface and version. Do not claim that Codex Desktop/App can consume a remote MCP server until the current app UI or official documentation confirms the path.

Use this decision path:

- If the app exposes MCP settings, configure the same HTTP URL and bearer-token environment variable pattern used for Codex CLI.
- If the app shares the active Codex configuration with CLI or IDE in the user's environment, configure `config.toml` and verify from the app.
- If the app does not expose direct MCP settings, explain the limitation and recommend Codex CLI/IDE for direct MCP use.
- If the user needs distribution through Codex App later, recommend a plugin as future packaging work, not part of this skill.

### Claude Code

Claude Code supports HTTP MCP servers. Use `claude mcp add` for the current user or project scope.

Example command using a shell variable instead of a literal token in shell history:

```bash
export BRAIN_MCP_URL="https://brain.example.com/mcp"
export BRAIN_MCP_TOKEN="<BRAIN_MCP_TOKEN>"
claude mcp add --transport http brain "$BRAIN_MCP_URL" --header "Authorization: Bearer $BRAIN_MCP_TOKEN"
```

For project-shared configuration, use Claude Code's project-scoped MCP configuration only when the token is not committed. Keep secrets in user-local settings, environment variables, or a secure helper supported by the client.

Verify inside Claude Code with:

```text
/mcp
```

Expected result: `brain` appears in the MCP server list. If authentication fails, re-check whether the token is a Brain MCP token, not `BRAIN_AUTH_TOKEN`.

### Claude Desktop

Claude Desktop has two relevant MCP paths:

- **Remote connectors through Claude account:** treat these like Claude Web. The Brain MCP URL must be reachable from Anthropic infrastructure.
- **Local Desktop MCP configuration:** use only when the installed Desktop version supports the needed transport and authentication headers.

When local Desktop configuration supports HTTP MCP servers with headers, use a configuration equivalent to:

```json
{
  "mcpServers": {
    "brain": {
      "type": "http",
      "url": "https://brain.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${BRAIN_MCP_TOKEN}"
      }
    }
  }
}
```

Do not commit this file if it contains a literal token. If the Desktop path uses a Claude account connector instead of local config, follow the Claude Web constraints below.

### Claude Web

Claude Web custom connectors use remote MCP. The server is contacted from Anthropic infrastructure, not from the user's local machine.

Before configuring Claude Web, confirm all of these:

- The Brain MCP URL is publicly reachable or allowlisted for the relevant Claude plan.
- TLS and DNS work from outside the user's private network.
- The connector path can supply authentication compatible with Brain's bearer-token MCP authentication.
- The user understands that exposing Brain publicly requires production-grade auth, monitoring, and secret handling.

If the current Claude Web connector flow cannot send Brain's required bearer token, do not weaken Brain authentication. Recommend one of these paths:

- Use Claude Code with an HTTP MCP server and bearer header.
- Use Claude Desktop local MCP configuration if the installed version supports it.
- Add a small trusted auth proxy or OAuth-capable connector layer as separate future work.

### Hermes

Treat Hermes as a trusted internal integration when it performs Brain curator workflows.

Configure Hermes with:

```bash
BRAIN_MCP_URL="https://brain.example.com/mcp"
BRAIN_MCP_TOKEN="<BRAIN_CURATOR_TOKEN>"
```

Use a curator token only when Hermes needs administrative MCP tools. If Hermes only needs ordinary client capabilities, create a Brain agent client and use its `brain_client_` token instead.

Do not confuse these Hermes-related values:

- `BRAIN_MCP_TOKEN`: bearer token Hermes sends to Brain MCP.
- `HERMES_WEBHOOK_SECRET`: secret Brain uses to sign outbox deliveries to Hermes.
- `HERMES_WEBHOOK_URL`: URL Brain uses to deliver events to Hermes.

Webhook settings do not authenticate Hermes to `/mcp`.

## Security Rules

Apply these rules every time:

- Never commit `.env`, local secret files, literal bearer tokens, or generated client tokens.
- Prefer environment variables, secure stores, or helper commands over static token values in config files.
- Use examples like `<BRAIN_MCP_TOKEN>` and `<BRAIN_CURATOR_TOKEN>` instead of real values.
- Validate the destination URL before attaching a token to it.
- Use client tokens for normal agents.
- Use curator tokens only for trusted administrative integrations.
- Explain Claude Web and remote Claude connector network exposure before recommending that path.
- Do not remove Brain authentication to make a client easier to connect.

## Verification

Use the strongest verification available for the selected client:

- Brain service:

  ```bash
  curl -fsS "$BRAIN_BASE_URL/health"
  ```

- Codex CLI/TUI:

  ```text
  /mcp
  ```

- Claude Code:

  ```text
  /mcp
  ```

- Claude Web or remote Claude connector:
  - Confirm the connector is added and authenticated in Claude settings.
  - Confirm the Brain URL is not `localhost` and is reachable from outside the private network.

- Hermes:
  - Confirm Hermes uses `BRAIN_MCP_URL` ending in `/mcp`.
  - Confirm Hermes uses the intended MCP bearer token, not webhook secrets.
  - Confirm any failed MCP call logs redact the token.

Do not treat a successful `/health` response as proof that MCP auth works. `/health` only proves that the HTTP service is reachable.

## Troubleshooting

Use this failure map:

| Symptom | Likely Cause | Action |
| --- | --- | --- |
| `401` from MCP | Missing token, wrong token, disabled client, wrong principal, or `BRAIN_AUTH_TOKEN` used for MCP | Use `BRAIN_CURATOR_TOKEN` for curator workflows or a `brain_client_` token for ordinary clients |
| `404` or route error | Client points at the wrong path | Use `<brain-base-url>/mcp` |
| `/health` works but MCP fails | Auth or MCP transport issue | Check bearer token, client MCP transport, and server logs |
| Claude Web cannot connect | Brain is local, private, behind VPN, blocked by firewall, or connector auth cannot send the bearer token | Use a public or allowlisted endpoint, Claude Code, local Desktop config, or future auth proxy work |
| Tools are missing | Principal lacks permission, client filtered tools, server did not reload, or wrong Brain environment is configured | Verify principal, enabled tools, client reload, and target URL |
| Hermes cannot administer Brain | Hermes is using a client token or webhook secret | Use curator MCP credentials for administrative workflows |
| Token appears in config diff | Secret was written literally | Remove it from the file, rotate the token if exposed, and switch to environment-variable or secure-store configuration |

## Output Expectations

When helping a user install Brain MCP:

- State the selected client path.
- State the exact MCP URL shape.
- State which token class is needed without printing the token.
- Provide only the configuration relevant to that client.
- Include a verification step.
- Include the most likely next diagnostic if verification fails.
`````

- [ ] **Step 5: Validate the skill structure**

Run:

```bash
python /root/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/install-brain-mcp
```

Expected output:

```text
Skill is valid!
```

- [ ] **Step 6: Verify file count and required client coverage**

Run:

```bash
find .agents/skills/install-brain-mcp -maxdepth 3 -type f | sort
rg -n "Codex CLI|Codex Desktop/App|Claude Desktop|Claude Web|Claude Code|Hermes" .agents/skills/install-brain-mcp/SKILL.md
rg -n "/mcp|/health|Authorization: Bearer|BRAIN_AUTH_TOKEN|BRAIN_CURATOR_TOKEN|brain_client_|curator|client" .agents/skills/install-brain-mcp/SKILL.md
```

Expected:

- `find` prints only `.agents/skills/install-brain-mcp/SKILL.md`.
- The client coverage search prints at least one line for every client named in the first `rg` command.
- The Brain fact search prints matches for MCP URL, health check, bearer auth, operational token separation, curator token, client token pattern, and principal names.

- [ ] **Step 7: Scan for scaffold markers, ellipses, and likely secrets**

Run:

```bash
rg -n "TO""DO|TB""D|FIX""ME|\\.\\.\\.|your""-token|sk-[A-Za-z0-9]|Bearer [A-Za-z0-9_-]{12,}|brain_client_[A-Za-z0-9_-]{8,}" .agents/skills/install-brain-mcp/SKILL.md
```

Expected:

- No output.
- Exit status is 1 because `rg` found no matches.

- [ ] **Step 8: Check formatting and staged diff**

Run:

```bash
git diff --check -- .agents/skills/install-brain-mcp/SKILL.md
git diff -- .agents/skills/install-brain-mcp/SKILL.md
```

Expected:

- `git diff --check` exits with status 0.
- The diff creates `.agents/skills/install-brain-mcp/SKILL.md`.
- The diff does not create `agents/openai.yaml`, scripts, references, or assets.

- [ ] **Step 9: Commit the skill**

Run:

```bash
git add .agents/skills/install-brain-mcp/SKILL.md
git commit -m "feat(skills): add brain mcp install skill"
```

Expected:

- Commit succeeds.
- The unrelated `README.md` deletion remains unstaged.

## Task 2: Final Verification

**Files:**
- Verify: `.agents/skills/install-brain-mcp/SKILL.md`

- [ ] **Step 1: Verify the committed skill exists**

Run:

```bash
git show --stat --oneline HEAD
git show --name-only --oneline HEAD
```

Expected:

- The latest commit message is `feat(skills): add brain mcp install skill`.
- The committed file list includes `.agents/skills/install-brain-mcp/SKILL.md`.
- No unrelated files are included in the commit.

- [ ] **Step 2: Re-run structural validation after commit**

Run:

```bash
python /root/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/install-brain-mcp
```

Expected output:

```text
Skill is valid!
```

- [ ] **Step 3: Confirm final working tree state**

Run:

```bash
git status --short
```

Expected:

- Only pre-existing unrelated changes remain, currently ` D README.md`.
- No `.agents/skills/install-brain-mcp` files are unstaged.

- [ ] **Step 4: Report completion**

Report:

```text
Created and validated `.agents/skills/install-brain-mcp/SKILL.md`.
Validation: `quick_validate.py` passed.
Committed as `<commit-hash> feat(skills): add brain mcp install skill`.
Unrelated pre-existing change left untouched: `README.md` deletion.
```
