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
   a value for `BRAIN_TRANSPORT_HMAC_SECRET`. `BRAIN_RUNTIME_HMAC_SECRET` is
   ignored since Amendment 2 and may be deleted from an installed file.
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

### Mandatory upstream integrity gate before Stage 3

Before the first Stage 3 deployment or restart of any Brain observer/plugin
component, capture the clean upstream Hermes baseline exactly once:

```sh
cd /root/brain
PYTHONPATH=src .venv/bin/python scripts/hermes_integrity.py capture \
  --repo /usr/local/lib/hermes-agent \
  --output /var/lib/brain/runtime/hermes-integrity-baseline.json
```

Immediately verify that baseline before installing or starting the observer:

```sh
cd /root/brain
PYTHONPATH=src .venv/bin/python scripts/hermes_integrity.py verify \
  --repo /usr/local/lib/hermes-agent \
  --baseline /var/lib/brain/runtime/hermes-integrity-baseline.json
```

Only a successful capture followed by a successful verify permits Stage 3 to
proceed. After deployment, second-device pairing, and the controlled E2E, run
the same verify command again. Any HEAD, worktree, manifest, file-type, or hash
mismatch stops the rollout. Never overwrite the baseline, repair Hermes,
normalize a dirty checkout, or change upstream automatically.

This byte/state integrity gate is distinct from
`scripts/hermes_integration_check.py`: integrity proves the original checkout
is unchanged, while compatibility proves the supported public APIs, schemas,
and runtime semantics.

## Hermes update gate

After every `hermes update`, run `scripts/smoke_test.py`. It checks the real
Hermes resolver, exact MCP header contract, placeholder interpolation,
`tools.include`, `no_mcp`, worker Task/Run environment exports, trusted gateway
auto-subscription source, database schema, transport and health endpoint.

If any security check fails, stop the rollout and disable Brain until the
compatibility issue is corrected. Do not use `hermes tools list --platform` as
proof of MCP containment; only the resolver check is authoritative.

The checker is read-only: it inspects the public plugin API, session ContextVars, state/Kanban schemas,
WhatsApp bridge batching (`"\n"` join and timer reset), adapter identity, and
delivery-ledger states. It never repairs Hermes or writes its databases.

## Implementation boundary

Implemented: Brain-owned runtime persistence, separate HMAC domains, safe
transport ingestion, fail-closed identity proof, contact-scoped
`conversation_context`, and compatibility checks. There is no upstream Hermes
modification.

The receive-only observer runtime is implemented with its own pinned Baileys,
its own session path, raw-data-free safe spool, bounded retry, loopback health,
and versioned systemd/env artifacts. Its production state belongs under
`/var/lib/brain/whatsapp-observer`, with its session at
`/var/lib/brain/whatsapp-observer/session`; it must remain separate from
`/root/.hermes/platforms/whatsapp/session`, which must never be copied, shared,
or disconnected by the observer.

The observer uses its own Node runtime under `/opt/brain/node`, pinned to Node
`v26.7.0`. It is independent from the Hermes-managed runtime at
`/root/.hermes/node`, which must not be reused by the observer. Observer Node
upgrades are explicit, separate maintenance operations.

Not implemented, and out of scope since Amendment 2 (2026-08-31): automated
FamaChat lifecycle writing. Brain no longer correlates Hermes turns,
reconstructs Kanban bindings, derives lifecycle state, or holds any FamaChat
credential, and there is no writer service to deploy. Reno owns the lifecycle
transitions through its own MCP surface, guarded by a mandatory
`expectedStatus` predicate enforced by FamaChat. The turn-correlation spine,
the lifecycle engine and the writer were removed rather than left dormant; see
section 2.5 and Amendment 2 of the design spec for why.

### Production observer gate

Perform Stage 3 only as a separately authorized controlled change:

1. Require a passing integrity baseline before deployment.
2. Create the private observer directories and secrets, install the dedicated
   service artifact, and verify its loopback-only health endpoint.
3. Pair manually as a **second linked device** using only the observer's own
   session path. Never disconnect, replace, copy, or reuse the Hermes session.
4. Run one controlled CTWA inbound flow and confirm durable Brain ingestion.
5. Confirm the resulting `conversation_context` correlation end to end without
   exposing message content or transport identity in the observer outbox.
6. Re-run the complete integrity baseline and verify Hermes coexistence after
   the controlled flow.

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
