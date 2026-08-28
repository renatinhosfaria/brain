# Brain

Brain is a localhost-only, read-only MCP service that gives Hermes workers
longitudinal history for the WhatsApp DM that created the current Kanban task.
It never accepts identity as a tool argument, never writes Hermes databases,
and exposes only `conversation_recent` and `conversation_search`.

## Run locally

Create the locked virtual environment, copy
[`deploy/brain.env.example`](deploy/brain.env.example) to `/etc/brain/brain.env`,
and put the two SHA-256 token digests in a TOML file based on
[`deploy/brain.toml.example`](deploy/brain.toml.example), then run:

Generate each digest interactively with `/root/brain/scripts/brain-token-hash`.

```sh
/root/.hermes/bin/uv sync --frozen
BRAIN_CONFIG=/etc/brain/brain.toml /root/brain/.venv/bin/brain
```

The service listens on `127.0.0.1:8765`, exposes MCP at `/mcp`, and exposes the
schema-guarded health endpoint at `/health`.

Follow [`docs/runbook.md`](docs/runbook.md) for installation, live WAL/SHM
validation, staged rollout and rollback. The unit intentionally does not use
`ProtectHome=true`.

## Hermes integration

Use [`deploy/hermes-brain.example.yaml`](deploy/hermes-brain.example.yaml) for
the worker Profile configuration and
[`docs/worker-history-invariant.md`](docs/worker-history-invariant.md) for the
SOUL.md addition. `no_mcp` is required on Telegram and other platforms that
must not inherit the global Brain server.

The worker example explicitly blocks Brain on Telegram and WhatsApp; an omitted
platform inherits globally enabled MCP servers in Hermes.

Run the post-update compatibility smoke test with:

```sh
/root/brain/.venv/bin/python scripts/smoke_test.py
```

The test suite uses `unittest` plus the installed project dependencies:

```sh
PYTHONPATH=src /root/brain/.venv/bin/python -m unittest discover -s tests -v
```
