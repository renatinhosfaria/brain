# Brain Plan 1 foundation

Brain is a localhost-only, capability-scoped service for Hermes. It reads
Hermes `state.db` and `kanban.db` without writing them and owns a separate
runtime database at `/var/lib/brain/runtime/brain-runtime.db`. Identity is
derived from trusted execution/session context, never accepted as a worker tool
argument. Plan 1 makes no upstream Hermes modification at
`/usr/local/lib/hermes-agent`.

The MCP surface has exactly three tools:

- `conversation_recent` and `conversation_search`: longitudinal history for
  Reno and FamaAgent, scoped by the current Task/Run and WhatsApp DM;
- `conversation_phone`: a zero-argument tool for Porteiro/Cadastro that returns
  a verified phone or the sanitized `phone_not_resolved` result.

The CEO does not receive the worker MCP server. The external
`integrations/hermes/brain-ceo-bridge/` plugin reads the current gateway
`ContextVar` session through the public accessor and registers no hooks. It
obtains bounded `conversation_context` for the contact on the other side of the
current WhatsApp DM. This keeps identity authority in Brain while allowing
`hermes update` without a core fork.

## Scope

Implemented: the Brain runtime DB, distinct runtime/transport HMAC domains,
safe transport ingestion with fail-closed identity proof, contact-scoped
`conversation_context`, and a read-only Hermes compatibility checker. The
runtime DB stores technical metadata and HMACs, not raw message bodies.

Out of scope since Amendment 2 (2026-08-31): automated CRM lifecycle writing.
Brain no longer correlates Hermes turns, reconstructs Kanban bindings, derives
lifecycle state, or holds any FamaChat credential. Reno owns the lifecycle
transitions and executes them through its own MCP surface, guarded by a
mandatory `expectedStatus` predicate that FamaChat enforces server-side. See
section 2.5 and Amendment 2 of the design spec.

The observer session path is reserved as
`/var/lib/brain/whatsapp-observer/session`; it must never share Hermes' session
at `/root/.hermes/platforms/whatsapp/session`.

## Run locally

Create the locked virtual environment, copy
[`deploy/brain.env.example`](deploy/brain.env.example) to `/etc/brain/brain.env`,
and put the distinct principal SHA-256 token digests in a TOML file based on
[`deploy/brain.toml.example`](deploy/brain.toml.example), then run:

Generate each digest interactively with `/root/brain/scripts/brain-token-hash`.

```sh
/root/.hermes/bin/uv sync --frozen
BRAIN_CONFIG=/etc/brain/brain.toml /root/brain/.venv/bin/brain
```

The service listens on `127.0.0.1:8765`, exposes MCP at `/mcp`, the private
gateway routes `/internal/gateway/conversation-phone` and
`/internal/gateway/conversation-context`, the private ingestion route
`/internal/transport/events`, and the schema-guarded health endpoint at
`/health`.

For an LID, Brain scans only the configured WhatsApp session directory and
accepts semantically validated `lid-mapping-{phone}.json` and
`lid-mapping-{lid}_reverse.json` evidence. Conflicts, malformed mappings,
symlinks, group/broadcast JIDs and missing mappings fail closed; Brain never
uses display names, message text, `session_key` text, `creds.json`, or key files
as identity.

Follow [`docs/runbook.md`](docs/runbook.md) for installation, live WAL/SHM
validation, staged rollout and rollback. The unit intentionally does not use
`ProtectHome=true`.

## Hermes integration

Use [`deploy/hermes-brain.example.yaml`](deploy/hermes-brain.example.yaml) for
Porteiro/Cadastro,
[`deploy/hermes-brain-memory.example.yaml`](deploy/hermes-brain-memory.example.yaml)
for Reno, and
[`deploy/hermes-brain-famaagent.example.yaml`](deploy/hermes-brain-famaagent.example.yaml)
for FamaAgent. Porteiro, Cadastro and Reno preserve the existing FamaChat MCP
server alongside Brain; FamaAgent keeps only Brain. All templates keep Brain
out of Telegram and WhatsApp via `no_mcp`; the worker's Task/Run headers are
server-derived and are not tool arguments. Follow
[`docs/worker-history-invariant.md`](docs/worker-history-invariant.md) for the
SOUL.md addition. Follow
[`docs/conversation-identity-invariant.md`](docs/conversation-identity-invariant.md)
for phone and session identity rules.

Install the CEO bridge only after reviewing it and
[`deploy/hermes-ceo-brain.example.yaml`](deploy/hermes-ceo-brain.example.yaml).
Copy the versioned source to `/root/.hermes/plugins/brain-ceo-bridge`, enable
`brain-ceo-bridge` in the CEO `plugins.enabled` list, and expose its
`brain-context` toolset on WhatsApp only. Its only tool is
`conversation_context`. Do not add `brain-context` to CLI or
Telegram, and do not configure the worker Brain MCP server in the CEO. The
installer and runbook configure `BRAIN_GATEWAY_TOKEN` for that plugin. This
production copy is intentionally outside this repository; development here
changes only `/root/brain`.

The deployment boundary is explicit: `/root/brain` contains the source and
examples, `/root/.hermes` receives only the operator's runtime installation,
and `/usr/local/lib/hermes-agent` is never edited.

Run the post-update compatibility smoke test with:

```sh
/root/brain/.venv/bin/python scripts/smoke_test.py
```

The test suite uses `unittest` plus the installed project dependencies:

```sh
PYTHONPATH=src /root/brain/.venv/bin/python -m unittest discover -s tests -v
```
