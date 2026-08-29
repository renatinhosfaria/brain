# Brain operations runbook

## Install or update

1. Confirm `/root/.hermes/state.db` and `/root/.hermes/kanban.db` exist, the
   WhatsApp session directory is `/root/.hermes/platforms/whatsapp/session`,
   and the CEO config has `kanban.auto_subscribe_on_create: true`.
2. On the production server, use the already-built Brain virtual environment;
   do not use `uv` there. Confirm it is usable with:

   ```sh
   cd /root/brain
   /root/brain/.venv/bin/python -c 'import brain, uvicorn'
   ```

   For development environments, the equivalent dependency setup is
   `uv sync --frozen` in `/root/brain`.
3. On first install, run `scripts/install_brain_secrets.py`. It creates four
   distinct worker tokens and one separate gateway token through Hermes' own
   secret writer. It installs only their SHA-256 digests plus a stable cursor
   secret under `/etc/brain`; raw worker tokens remain in their Profile scopes
   and `BRAIN_GATEWAY_TOKEN` remains in the CEO scope. For an intentional
   rotation, rerun with `--force` and restart Brain.
4. Confirm `/etc/brain/brain.toml` and `/etc/brain/brain.env` are owned by root,
   mode `0600`, and the Reno/FamaAgent `.env` files remain mode `0600`. Create
   `/var/lib/brain/runtime` with mode `0700`; keep
   `/var/lib/brain/runtime/brain-runtime.db` mode `0600`. Configure distinct
   values for `BRAIN_RUNTIME_HMAC_SECRET` and `BRAIN_TRANSPORT_HMAC_SECRET`.
5. Merge `deploy/hermes-brain.example.yaml` into Porteiro and Cadastro, and
   preserve `famachat` in their CLI toolsets. Merge
   `deploy/hermes-brain-memory.example.yaml` into Reno, preserving `famachat`,
   and `deploy/hermes-brain-famaagent.example.yaml` into FamaAgent, without
   adding `famachat`. Put each raw, distinct `BRAIN_TOKEN` only in its
   Profile's secret scope. Keep `no_mcp` on worker Telegram/WhatsApp toolsets.
6. Review the versioned
   `integrations/hermes/brain-ceo-bridge/` source and
   `deploy/hermes-ceo-brain.example.yaml`, copy the source to
   `/root/.hermes/plugins/brain-ceo-bridge`, enable `brain-ceo-bridge` in the
   CEO `plugins.enabled` list, and expose `brain-context` on WhatsApp only.
   Do not add it to CEO CLI/Telegram and do not configure the worker Brain MCP
   server in the CEO. Ensure only the CEO has `BRAIN_GATEWAY_TOKEN`. These
   production paths are not modified by this repository task.
7. Add `docs/worker-history-invariant.md` and
   `docs/conversation-identity-invariant.md` to both worker `SOUL.md` files.
8. Install `deploy/brain.service` as `/etc/systemd/system/brain.service`, run
   `systemctl daemon-reload`, then enable and start it.

## Deployment

If no cursor secret is configured (`BRAIN_CURSOR_SECRET` or
`server.cursor_secret` in `/etc/brain/brain.toml`), Brain generates a secret at
startup. Each process then has its own key, so cursors do not survive a restart
and Brain must run as a single process. Running Brain with more than one worker
makes pagination fail intermittently: a cursor emitted by one process is
rejected by another. For a multi-process deployment, `BRAIN_CURSOR_SECRET` is
mandatory and must be the same stable value in every process.

### Deployment note for WAL/SHM coverage

The test `test_readonly_reader_observes_recent_uncheckpointed_wal_content` uses
fixture databases created by the test process. It proves SQLite semantics, not
filesystem permission behavior: reading the `-shm` file of a database written by
another process, with mode `0600` and owner `root`, currently works because the
service runs as `root`. Any migration to a dedicated Unix user invalidates this
coverage and requires a new test against Hermes' live databases with the gateway
running. That test must write a message through the gateway and confirm that
Brain reads it immediately afterward.

## Required validation

Run, in order:

Production server (the Brain venv is already built; no `uv` required):

```sh
cd /root/brain
PYTHONPATH=src /root/brain/.venv/bin/python -m unittest discover -s tests -v
/root/brain/.venv/bin/python scripts/hermes_integration_check.py
/root/brain/.venv/bin/python scripts/smoke_test.py
```

Development alternative (with `uv` available):

```sh
cd /root/brain
uv run python -m unittest discover -s tests -v
uv run python scripts/hermes_integration_check.py
uv run python scripts/smoke_test.py
```

Confirm `curl -fsS http://127.0.0.1:8765/health` returns the exact eight-field
compatible payload:

```json
{"status":"ok","hermes_state_db":"ok","hermes_kanban_db":"ok","runtime_db":"ok","whatsapp_identity":"compatible","gateway_bridge":"configured","schema":"compatible","hermes_compatibility":"compatible"}
```

Confirm the endpoint is not listening on a non-loopback address. Also verify a
known LID mapping, a missing mapping, and two concurrent contexts for different
contacts before enabling phone-dependent work.

For the WAL/SHM gate, send a synthetic message through the real WhatsApp
gateway and immediately retrieve it through a synthetic authorized Kanban run.
This proves the reader sees live WAL content rather than only the last
checkpoint. Do not enable `ProtectSystem=strict` until this test passes with the
gateway active.

## Hermes update gate

After every `hermes update`, run `scripts/smoke_test.py`. It checks the real
Hermes resolver, exact MCP header contract, placeholder interpolation,
`tools.include`, `no_mcp`, worker Task/Run environment exports, trusted gateway
auto-subscription source, database schema, transport and health endpoint.

If any security check fails, stop the rollout and disable Brain until the
compatibility issue is corrected. Do not use `hermes tools list --platform` as
proof of MCP containment; only the resolver check is authoritative.

The checker is read-only: it inspects public plugin/hook APIs, `turn_id` and
argument-modification contracts, session ContextVars, state/Kanban schemas,
WhatsApp bridge batching (`"\n"` join and timer reset), adapter identity, and
delivery-ledger states. It never repairs Hermes or writes its databases.

## Plan 1 boundary

Implemented: Brain-owned runtime persistence, separate HMAC domains, safe
transport ingestion, fail-closed identity proof, turn registration,
deterministic batch correlation, `conversation_context`, public Hermes hooks,
Kanban idempotency, and compatibility checks. There is no upstream Hermes
modification.

Not implemented: a production observer service, lifecycle engine, lifecycle
writer, FamaChat CRM automation, or production write enablement. Reserve
`/var/lib/brain/whatsapp-observer/session` for the future observer and keep it
separate from `/root/.hermes/platforms/whatsapp/session`; never copy or share
the Hermes session directory. No observer unit is installed by Plan 1.

## Degradation and rollback

If Brain is unavailable, workers continue from the current task without
historical context and must not use `session_search`, terminal or direct SQLite
as fallback. If `conversation_phone` is unavailable, do not guess a phone from
history or from the mapping path; return to the controlled operational flow.

To roll back all V2 exposure, disable `brain-ceo-bridge`, remove
`brain-context` from the CEO WhatsApp toolset, remove `brain` from the CLI
toolsets of Porteiro, Cadastro, Reno and FamaAgent, and restart the affected
processes. Keep `no_mcp` on worker Telegram and WhatsApp. Restore the prior
Porteiro/Cadastro behavior for a missing phone (controlled block; never infer)
and keep the worker Profiles' Brain MCP entries disabled. Stopping
`brain.service` is safe: Brain owns no transcript and writes no Hermes domain
data.

Rollback does not require reverting files under `/usr/local/lib/hermes-agent`;
those files are never deployment targets for Brain. Brain rollback and
Hermes-core rollback do not require reverting the same artifacts.

Never put tokens, Authorization headers, transcript text, phone numbers,
`chat_id`, `session_key` or database paths in incident notes or logs.
