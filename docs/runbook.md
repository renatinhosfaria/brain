# Brain operations runbook

## Install or update

1. Confirm `/root/.hermes/state.db` and `/root/.hermes/kanban.db` exist and the
   CEO config has `kanban.auto_subscribe_on_create: true`.
2. Run `/root/.hermes/bin/uv sync --frozen` in `/root/brain`.
3. On first install, run `scripts/install_brain_secrets.py`. It creates distinct
   Profile tokens through Hermes' own secret writer and installs only their
   SHA-256 digests plus a stable cursor secret under `/etc/brain`. For an
   intentional rotation, rerun with `--force` and restart Brain.
4. Confirm `/etc/brain/brain.toml` and `/etc/brain/brain.env` are owned by root,
   mode `0600`, and the Reno/FamaAgent `.env` files remain mode `0600`.
5. Merge `deploy/hermes-brain.example.yaml` into Reno and FamaAgent. Put each
   raw, distinct `BRAIN_TOKEN` only in that Profile's secret scope. Do not add
   Brain to the CEO Profile.
6. Add `docs/worker-history-invariant.md` to both worker `SOUL.md` files.
7. Install `deploy/brain.service` as `/etc/systemd/system/brain.service`, run
   `systemctl daemon-reload`, then enable and start it.

## Required validation

Run, in order:

```sh
cd /root/brain
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
/usr/local/lib/hermes-agent/venv/bin/python scripts/hermes_integration_check.py
.venv/bin/python scripts/smoke_test.py
```

Confirm `curl -fsS http://127.0.0.1:8765/health` returns the exact compatible
payload. Confirm the endpoint is not listening on a non-loopback address.

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

## Degradation and rollback

If Brain is unavailable, workers continue from the current task without
historical context and must not use `session_search`, terminal or direct SQLite
as fallback.

To roll back exposure, remove `brain` from Reno/FamaAgent `cli` toolsets while
leaving `no_mcp` on Telegram and WhatsApp, then restart their worker processes.
Stopping `brain.service` is safe: Brain owns no transcript and writes no Hermes
domain data.

Never put tokens, Authorization headers, transcript text, phone numbers,
`chat_id`, `session_key` or database paths in incident notes or logs.
