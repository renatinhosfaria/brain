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
7. Install `integrations/hermes/fama-whatsapp-human-handover/` as
   `/root/.hermes/plugins/fama-whatsapp-human-handover`, add the plugin name to
   the CEO `plugins.enabled` list, and set these values in the CEO `.env`:

   ```dotenv
   WHATSAPP_FORWARD_OWNER_MESSAGES=true
   FAMA_HANDOVER_TELEGRAM_CHAT_ID=-1004374717222
   FAMA_HANDOVER_TELEGRAM_THREAD_ID=1
   FAMA_HANDOVER_TELEGRAM_USER_ID=8564576789
   ```

   A manual WhatsApp Business/Web reply pauses only that contact and keeps both
   sides flowing into the canonical Hermes transcript without an LLM turn.
   The pause survives restarts. Only the configured administrator, chat and
   topic may run `/retomar <telefone>`. Resume does not send a WhatsApp message
   and waits for the customer's next inbound message before the CEO responds.
8. Add `docs/worker-history-invariant.md` and
   `docs/conversation-identity-invariant.md` to both worker `SOUL.md` files.
9. Install `deploy/brain.service` as `/etc/systemd/system/brain.service`, run
   `systemctl daemon-reload`, then enable and start it.

## Amendment 4: Meta Ads MCP authentication and rollout (2026-09-02)

Brain uses only the read-only Ads MCP endpoint
`https://mcp.facebook.com/ads`, pinned in code, for the one account
`act_1598606388477916`. Never add a Graph API fallback, a second account, or a
write tool. Meta-provided names, URLs, IDs, and responses are untrusted
evidence; never paste them into chat, tickets, command arguments, or logs.

### Authenticate and install

1. Open [Meta Developer Apps](https://developers.facebook.com/apps/) and create
   or reuse the business-owned app.
2. Add **Create & manage ads with Ads MCP server**.
3. Authorize the business user for `act_1598606388477916` only.
4. Select read access when Meta offers a Read/Manage choice.
5. Obtain the programmatic user token with the app OAuth flow or
   [Graph API Explorer](https://developers.facebook.com/tools/explorer/) and
   only the Ads MCP scopes Meta requires.
6. Install it from stdin or a TTY; never place the token in shell arguments,
   Git, chat, stdout, stderr, or logs:

   ```sh
   cd /root/brain
   /root/brain/.venv/bin/python scripts/install_meta_ads_credential.py \
     --config-dir /etc/brain --expires-at 2026-11-01T00:00:00Z
   ```

   The installer creates or atomically updates `/etc/brain/brain.env` with
   mode `0600` in a `0700` directory, refuses symlinks, preserves unrelated
   keys, and refuses a credential replacement unless `--rotate` is explicit.
7. Keep `meta_attribution_enabled = false`. Supply one known ad ID only via
   `BRAIN_META_PROBE_AD_ID` in the process environment and run
   `scripts/meta_ads_mcp_probe.py`. Its success line proves read-only tools,
   account access, and that optional known-ad read; it prints no returned data.

Meta's official references are the [Ads MCP overview](https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/ads-mcp-server-overview),
[setup and authentication](https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/ads-mcp-server-get-started),
[tool inventory](https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/ads-mcp-server-tools-ad-creation-and-management),
and [MCP security guidance](https://developers.facebook.com/documentation/mcp).

A `401` or `403` usually indicates a wrong business user, expired token, or
missing Ads MCP scope such as `ads_management`; diagnose the configuration and
scope in Meta, but do not compensate by adding a write tool or revealing the
credential.

### Rotation, pending work, and rollback

Rotate before the 14-, 7-, and 3-day health warnings. Install the replacement
with `--rotate`, run the authenticated probe, restart Brain, then revoke the
replaced token only after the new probe is successful. If a rotation or restart
fails, restore the root-only `brain.env` snapshot made before the change,
restart with `meta_attribution_enabled = false`, and investigate without
printing secret material.

`meta_ads_attribution` health is `disabled` when the feature is off, `ready`
after a valid probe, or `degraded` with a bounded reason when Meta is unavailable.
`pending` attribution is not evidence of an ad or campaign and never blocks
the CEO. Keep additive tables and raw CTWA evidence through their normal
90-day retention; catalog GC removes only unreferenced metadata unseen for more
than 90 days. During staged rollout: deploy disabled, probe, complete the
initial sync, verify a synthetic exact-match/mismatch/pending/retry case, then
enable the feature and send one controlled CTWA message. Disabling the flag is
the rollback; it stops Meta work without deleting raw attribution or tables.

## Amendment 3: raw CTWA attribution rollout (2026-09-02)

Raw `externalAdReply` is retained as plaintext attribution evidence for the
transport retention period and is returned only through the authenticated CEO
WhatsApp DM context. The observer spool and quarantine retain raw data for at
most 72 hours; persisted `transport_events` and their raw attribution are
retained for at most 90 days by default; `conversation_context` returns only a
recent six-hour window. It remains untrusted data and never changes transport
or lifecycle semantics by itself. This supersedes only earlier privacy language
that prohibited storing the complete `externalAdReply`; it does not revise
historical evidence, the identity boundary, or the observer's receive-only
role.

The raw field is captured whenever Baileys decodes an `externalAdReply`,
including events whose normalized evidence remains `ordinary_inbound`.
Normalized evidence alone controls classification. `conversation_context`
exposes the raw field as `events[].external_ad_reply` for CTWA candidates and
returns it as `null` for ordinary or legacy events. This is not a Meta Ads
Manager integration: campaign lookup, reporting, enrichment, and any control
plane action in Meta Ads remain out of scope.

### Settings and storage handling

Set the observer limits in `/etc/brain/whatsapp-observer.env`, owned by root
and mode `0600`:

```dotenv
BRAIN_CTWA_RAW_MAX_BYTES=4194304
BRAIN_CTWA_RAW_MAX_DEPTH=32
BRAIN_CTWA_RAW_MAX_NODES=10000
BRAIN_CTWA_QUARANTINE_MAX_BYTES=33554432
```

The raw JSON contract is lossless where ordinary JSON cannot represent the
WhatsApp value exactly. Bytes are encoded as
`{ "$type":"bytes", "encoding":"base64", "data":"..." }`; integers are
encoded as `{ "$type":"integer", "encoding":"decimal", "data":"..." }`.
`$type` names the original value family, `encoding` names the serialization,
and `data` holds base64 bytes or base-10 integer text. Do not alter, coerce, or
interpret these tag objects as instructions during inspection.

The runtime database and observer spool now hold plaintext attribution
evidence. Keep `/var/lib/brain/runtime` and
`/var/lib/brain/whatsapp-observer` private (`0700` directories, database and
environment files `0600`), do not copy their contents into tickets, command
output, or logs, and do not use a filesystem backup that broadens access
without an approved retention and access-control review. Raw attribution ages
out with its containing transport event under `transport_retention_days` (at
most 90 days by default); spool and quarantine records age out independently at
72 hours or sooner.

### Quarantine and complete-response failures

If raw capture violates a byte, depth, node, or canonical-JSON bound, the
observer must quarantine a content-free failure record (`event_id`, capture
time, and reason). It may retain the encoded raw object only when the complete
quarantine record fits the 32 MiB ceiling. Inspect quarantine by event ID,
timestamp, and reason only; never print, decode, or paste raw content into an
incident. If quarantine persistence itself fails, record only the fixed reason
`quarantine_persistence_failed` and alert on the observer health counter.

Do not send a partial CTWA attribution event after a capture failure. A context
response that exceeds the bridge's 32 MiB complete-response limit, or fails
raw validation, is `status=unavailable`; do not trim individual raw fields,
retry with a weakened boundary, or reconstruct attribution from logs.

### Rollout and rollback order

Roll out in this order in one authorized window: (1) validate the reviewed
Brain source and observer tests, (2) install the matching observer environment
and Brain configuration, (3) install the matching CEO bridge, (4) restart
Brain, then the observer, then the Hermes gateway, and (5) verify one
controlled CTWA flow through `conversation_context` without displaying raw
content. Ensure the CEO SOUL rule is present before the gateway restart.

For rollback, stop exposure first: remove `brain-context` from the CEO
WhatsApp toolset or disable `brain-ceo-bridge`, then restart the Hermes gateway.
Next stop the observer and Brain only if operationally required. Preserve the
runtime database for its explicit 90-day transport retention period and retain
spool/quarantine only until their 72-hour bound; never delete either merely to
hide a failure. Re-enable only with a reviewed, compatible Brain/observer/bridge
bundle and the CEO trust-boundary rule.

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
(cd observers/whatsapp && /opt/brain/node/bin/node --test "test/*.test.mjs")
/root/brain/.venv/bin/python scripts/hermes_integration_check.py
/root/brain/.venv/bin/python scripts/smoke_test.py
```

Development alternative (with `uv` available):

```sh
cd /root/brain
uv run python -m unittest discover -s tests -v
(cd observers/whatsapp && node --test "test/*.test.mjs")
uv run python scripts/hermes_integration_check.py
uv run python scripts/smoke_test.py
```

Run it with `/opt/brain/node`, the runtime the service itself executes. The
Hermes-managed runtime at `/root/.hermes/node` must not be reused: both are
`v26.7.0` today, and a pin whose two sides are only coincidentally equal proves
nothing on the day one of them moves.

The observer suite is not optional. It is the only thing that exercises the
contract between the Node normalizer and the Python resolver, and on
2026-09-01 it was the only suite that noticed `RuntimeIds` had lost an
argument — the 386 Python tests were all green while the cross-language probe
had been broken for a day.

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

## Stage 0 baseline

Recorded before any change, and re-recorded before any window, so "what did
this machine look like" is a file rather than a memory. A baseline that lives
only in a conversation cannot be compared against later.

```sh
OUT=/var/lib/brain/runtime/stage0-baseline.json
install -d -m 700 "$(dirname "$OUT")"
{
  printf '{\n'
  printf '  "recorded_at": "%s",\n' "$(date -Iseconds)"
  printf '  "brain": {"head": "%s", "branch": "%s", "dirty_lines": %s},\n' \
    "$(git -C /root/brain rev-parse HEAD)" \
    "$(git -C /root/brain rev-parse --abbrev-ref HEAD)" \
    "$(git -C /root/brain status --porcelain | wc -l)"
  printf '  "hermes-operational": {"head": "%s", "branch": "%s", "dirty_lines": %s},\n' \
    "$(git -C /root/.hermes rev-parse HEAD)" \
    "$(git -C /root/.hermes rev-parse --abbrev-ref HEAD)" \
    "$(git -C /root/.hermes status --porcelain | wc -l)"
  printf '  "hermes-upstream": {"head": "%s", "dirty_lines": %s},\n' \
    "$(git -C /usr/local/lib/hermes-agent rev-parse HEAD)" \
    "$(git -C /usr/local/lib/hermes-agent status --porcelain | wc -l)"
  printf '  "brain_health": %s,\n' "$(curl -fsS --max-time 5 http://127.0.0.1:8765/health)"
  printf '  "observer_health": %s\n' "$(curl -fsS --max-time 5 http://127.0.0.1:8775/health)"
  printf '}\n'
} > "$OUT"
chmod 600 "$OUT"
```

Every `dirty_lines` must be `0`. A dirty tree means the recorded HEAD does not
describe the machine, which is the whole value of the record. `/root/.hermes`
reported five dirty lines until 2026-09-01, when the skill curator's backup
directories were finally ignored.

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

The checker is read-only: it inspects the public plugin registration API,
session ContextVars, the state/Kanban schemas, WhatsApp identity mapping, the
resolver, and both copies of each CEO plugin. It never repairs Hermes or writes
its databases. It no longer inspects bridge batching, adapter debounce or
delivery-ledger states: those contracts belonged to turn correlation and to
proving the first T1 send, and Amendment 2 removed both.

### Bracketing a `hermes update`

An update moves the upstream installation and nothing else should move with it,
but it also restarts the gateway on its own, so anything it did change is
already live before anyone looks. `scripts/hermes_update_guard.py` records the
Fama-owned files beforehand and names what differs afterwards, separating the
expected movement from the alarming kind.

```sh
cd /root/brain
.venv/bin/python scripts/hermes_update_guard.py before \
    --out /var/lib/brain/runtime/staging/pre-update.json

hermes update

.venv/bin/python scripts/hermes_update_guard.py after \
    --snapshot /var/lib/brain/runtime/staging/pre-update.json
```

It watches both CEO plugins' source files, every Profile's `config.yaml` and
`SOUL.md`, the CEO's own SOUL and skill, `verify_team.py`, Reno's FamaChat
allowlist and `/etc/brain/brain.toml`. A moved upstream HEAD is reported as
expected, together with the reminder that the integrity baseline no longer
applies; an upstream that did **not** move is reported as a finding, because
the likeliest explanation is that the update never ran.

The one risk this cannot rule out in advance is Profile config migration. The
update migrates `config.yaml` across `_config_version` (39 at the time of
writing) using a deep merge, so user overrides such as Reno's `tools.include`
should survive — but that is read from the upstream source, not proven by
running it. The guard is what settles it, and `verify_team.py` is what catches
the consequence if the merge ever stops preserving them.

Then, in order: `verify_team.py full`, `hermes_integration_check.py`,
`smoke_test.py`, and the re-baselining below **last**. Integrity is
re-established only once the rest has confirmed the installation still behaves
as Brain expects; doing it first would attest a tree nobody had checked.

### Re-baselining after an authorized Hermes update

An update moves the upstream HEAD, so `hermes_integrity.py verify` will fail
with `HEAD_MISMATCH` from then on, permanently and correctly: the installation
is no longer the one that was baselined. The gate must be re-established, never
ignored, and `capture` refuses to overwrite an existing baseline precisely so
that re-establishing it is a deliberate act.

Do this only immediately after an update you performed yourself, from a clean
upstream worktree. A baseline captured at any other moment records whatever the
installation happens to contain, including a change nobody authorized — the
control can only ever attest that the tree has not moved since it was trusted,
so the moment of trust must be one you can vouch for.

```sh
cd /root/brain
git -C /usr/local/lib/hermes-agent status --porcelain   # must be empty
git -C /usr/local/lib/hermes-agent rev-parse HEAD       # record the new HEAD

BASE=/var/lib/brain/runtime/hermes-integrity-baseline.json
mv "$BASE" "$BASE.$(date +%Y%m%d-%H%M%S).superseded"

PYTHONPATH=src .venv/bin/python scripts/hermes_integrity.py capture \
  --repo /usr/local/lib/hermes-agent --output "$BASE"
PYTHONPATH=src .venv/bin/python scripts/hermes_integrity.py verify \
  --repo /usr/local/lib/hermes-agent --baseline "$BASE"
```

The superseded file is renamed rather than deleted, so the chain of what was
trusted when survives. Then re-run `scripts/smoke_test.py` and
`scripts/hermes_integration_check.py`: integrity proves the tree is unchanged
since capture, compatibility proves the APIs Brain depends on still behave, and
an update is exactly the event that can satisfy one while breaking the other.

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

For a WhatsApp handover-only rollback, disable `fama-whatsapp-human-handover`
and set
`WHATSAPP_FORWARD_OWNER_MESSAGES=false` in the same gateway restart. Retain
`$HERMES_HOME/plugin-data/fama-whatsapp-human-handover/handover.db` by default
so paused-contact state remains recoverable and auditable. Delete that database
only after an explicit, permanent decommission decision; disabling the plugin
does not require deleting it.

Rollback does not require reverting files under `/usr/local/lib/hermes-agent`;
those files are never deployment targets for Brain. Brain rollback and
Hermes-core rollback do not require reverting the same artifacts.

Never put tokens, Authorization headers, transcript text, phone numbers,
`chat_id`, `session_key` or database paths in incident notes or logs.

## Amendment 2 deploy window

Brain's code, the installed CEO plugin, and `/etc/brain/brain.toml` form one
bundle and must move together in a single authorized window.

The config is read at startup, so editing the TOML does not disturb the process
already running. The risk is the restart: whichever artefact is stale when the
service comes back is the one that breaks it.

- restarting with the new config but the old code leaves the CEO without
  `turn_register`, which the old code still requires;
- restarting with the new code but the old config leaves a principal granting a
  capability the new code rejects, and `hermes_integration_check.py` red;
- restarting Brain with the new code while the gateway still holds the hooked
  plugin leaves those hooks calling a route that no longer exists.

`git checkout` alone is a deployment here: `brain.service` runs
`/root/brain/.venv/bin/brain` directly from the working tree, so switching
branches changes what the next restart executes. Treat a branch switch on this
machine as an artefact change, not an editing convenience.

### The bundle

A bundle is three artefacts derived from one reviewed commit, and it is the
only unit that is ever deployed or restored:

| Artefact | Post-Amendment-2 content |
| --- | --- |
| `/root/brain` working tree | no writer, no FamaChat client, no correlation |
| `/root/.hermes/plugins/brain-ceo-bridge` | one tool, zero hooks |
| `/etc/brain/brain.toml` | `default` principal grants only `conversation_context` |

Never install or restore one of these without the other two. Every failure
mode above is a mixed set, and each stays silent until the next restart.

Bundles are built and rotated by `scripts/brain_bundle.py`, which keeps three
slots under `/var/lib/brain/runtime/bundles`:

```text
candidate   what review approved and the next window will install
active      what the running service was deployed from
previous    what active was before the last promotion
```

A bundle is identified by the **full** 40-character commit SHA, because a slot
must name exactly one tree and abbreviations can collide. Creation refuses a
dirty worktree — a bundle built from uncommitted edits cannot be rebuilt, so
it can never be verified and `previous` would be a promise the repository
cannot keep. It also refuses a config that still grants `turn_register` or
still declares a writer principal; it validates that file and never edits it.

### Before the window

Prepare the post-Amendment-2 config, then build and verify the candidate:

The prepared config is written **outside** the repository. Writing it inside
`/root/brain` dirties the worktree, and `create` then refuses the very sequence
this runbook prescribes:

```sh
install -d -m 700 /var/lib/brain/runtime/staging
sed 's/tools = \["conversation_context", "turn_register"\]/tools = ["conversation_context"]/' \
    /etc/brain/brain.toml > /var/lib/brain/runtime/staging/brain.toml.next
chmod 600 /var/lib/brain/runtime/staging/brain.toml.next

cd /root/brain
PYTHONPATH=src .venv/bin/python scripts/brain_bundle.py create \
    --config /var/lib/brain/runtime/staging/brain.toml.next
PYTHONPATH=src .venv/bin/python scripts/brain_bundle.py verify candidate
PYTHONPATH=src .venv/bin/python scripts/brain_bundle.py status
```

Confirm the generated config keeps every token digest unchanged. `create` fails
loudly if the config or the worktree is not ready, which is the point: the
bundle cannot be assembled from a half-prepared machine. Re-running `create` on
an unchanged commit is safe and returns the same bundle; if anything about the
inputs changed, it refuses rather than rewriting a bundle someone may already
have verified.

### The window

Let `BUNDLE=/var/lib/brain/runtime/bundles/candidate`.

1. Check out the candidate's commit in `/root/brain`.
2. Replace `/root/.hermes/plugins/brain-ceo-bridge/` with `$BUNDLE/plugin`.
   Stage the new copy beside the live one and swap by rename, restoring the
   old directory if the swap fails — never delete the live plugin before the
   replacement is fully in place:

   ```sh
   SOURCE=/var/lib/brain/runtime/bundles/candidate
   ```

   ```sh
   set -euo pipefail
PLUGINS=${PLUGINS:-/root/.hermes/plugins}
LIVE="$PLUGINS/brain-ceo-bridge"

# A residual .old is the fingerprint of an earlier swap that did not finish.
# Overwriting it would destroy the only copy of a plugin nobody restored.
if [ -e "$LIVE.old" ]; then
    echo "refusing: $LIVE.old exists from an earlier failed swap" >&2
    exit 1
fi

# Stage beside the live plugin, and guard every step that could leave a
# partial copy in place: a half-written bridge loads and fails at runtime
# rather than at install time.
rm -rf "$LIVE.new"
if ! cp -a "$SOURCE/plugin" "$LIVE.new"; then
    rm -rf "$LIVE.new"
    echo "copy failed; live plugin untouched" >&2
    exit 1
fi
if ! mv "$LIVE" "$LIVE.old"; then
    rm -rf "$LIVE.new"
    echo "could not set aside the live plugin; nothing changed" >&2
    exit 1
fi
if ! mv "$LIVE.new" "$LIVE"; then
    mv "$LIVE.old" "$LIVE"
    rm -rf "$LIVE.new"
    echo "swap failed; previous plugin restored" >&2
    exit 1
fi
rm -rf "$LIVE.old"
   ```

   The rename also replaces the whole directory at once, so no stale source
   file survives from the old copy.
3. Install `$BUNDLE/brain.toml` as `/etc/brain/brain.toml`, mode 0600.
4. Only once all three are in place, restart `brain.service`, then the Hermes
   gateway so the CEO reloads the plugin.
5. Run the validation block below in full.
6. Only after every gate passes, record the deployment, as described
   under **Record the deployment**.

#### Validation block

Every check below must pass against the live system, not a copy:

```sh
/usr/local/lib/hermes-agent/venv/bin/python -c "import sys; \
  sys.path.insert(0, '/usr/local/lib/hermes-agent'); \
  from hermes_cli.plugin_dev import doctor_plugin; \
  r = doctor_plugin('/root/.hermes/plugins/brain-ceo-bridge'); \
  print(r.ok, sorted(r.registered_tools), sorted(r.registered_hooks))"

cd /root/brain
.venv/bin/python scripts/hermes_integration_check.py
.venv/bin/python scripts/smoke_test.py
PYTHONPATH=src .venv/bin/python scripts/hermes_integrity.py verify \
  --repo /usr/local/lib/hermes-agent \
  --baseline /var/lib/brain/runtime/hermes-integrity-baseline.json
```

The plugin doctor must report zero hooks. `hermes_integration_check.py` doctors
the **installed** plugin as well as the versioned source and fails on any byte
difference between them, because a plugin that passes in the repository while
the gateway loads something else is exactly the drift that went unnoticed on
2026-08-31.

Finish with one controlled CTWA and confirm the CEO receives contact-scoped
context from a real inbound.

#### Record the deployment

Only now, with every gate above green, does the authoritative state move:

```sh
PYTHONPATH=src .venv/bin/python scripts/brain_bundle.py promote
```

Recording is the last step, never the first. Until it runs, the state still
names the previous deployment, which is the correct thing for it to say while
the new one is unproven. `promote` verifies the candidate before rotating, so a
corrupt bundle can never displace a good `active`, and it writes `active` and
`previous` in one atomic operation rather than as two separate links.

### Partial-deploy recovery

If the window is interrupted — the plugin copied but the config missed, a
restart taken before all three artefacts were in place — the fix is to finish
applying the same bundle, not to undo anything:

```sh
SOURCE=/var/lib/brain/runtime/bundles/candidate
git checkout "$(basename "$(readlink -f "$SOURCE")")"
```

Swap the plugin without ever deleting the live copy first:

```sh
set -euo pipefail
PLUGINS=${PLUGINS:-/root/.hermes/plugins}
LIVE="$PLUGINS/brain-ceo-bridge"

# A residual .old is the fingerprint of an earlier swap that did not finish.
# Overwriting it would destroy the only copy of a plugin nobody restored.
if [ -e "$LIVE.old" ]; then
    echo "refusing: $LIVE.old exists from an earlier failed swap" >&2
    exit 1
fi

# Stage beside the live plugin, and guard every step that could leave a
# partial copy in place: a half-written bridge loads and fails at runtime
# rather than at install time.
rm -rf "$LIVE.new"
if ! cp -a "$SOURCE/plugin" "$LIVE.new"; then
    rm -rf "$LIVE.new"
    echo "copy failed; live plugin untouched" >&2
    exit 1
fi
if ! mv "$LIVE" "$LIVE.old"; then
    rm -rf "$LIVE.new"
    echo "could not set aside the live plugin; nothing changed" >&2
    exit 1
fi
if ! mv "$LIVE.new" "$LIVE"; then
    mv "$LIVE.old" "$LIVE"
    rm -rf "$LIVE.new"
    echo "swap failed; previous plugin restored" >&2
    exit 1
fi
rm -rf "$LIVE.old"
```

Then the config and the restart:

```sh
install -m 600 "$SOURCE/brain.toml" /etc/brain/brain.toml
systemctl restart brain.service
```

Then restart the Hermes gateway and re-run the validation block. This is
recovery, not rollback: the target is the bundle that was already being
installed.

### Rollback

**The first post-Amendment-2 release has no compatible rollback.** `previous`
is unset because no earlier bundle of this architecture was ever deployed, and
the artefacts currently on the machine are not one: they are writer-era code, a
plugin registering three hooks, and a principal granting `turn_register`.
Restoring them is not a rollback, it is a different architecture.

So for this window there are exactly two responses to a failure:

- **roll forward** — diagnose, fix on the branch, build a new candidate, and
  run the window again; this is the default;
- **architectural reversion** — explicitly authorized, described below.

From the second release onward, `previous` names a real bundle of this
architecture and rollback becomes ordinary:

```sh
cd /root/brain
PYTHONPATH=src .venv/bin/python scripts/brain_bundle.py plan-rollback \
    --out /var/lib/brain/runtime/staging/rollback-plan.json
```

`plan-rollback` verifies the previous bundle and writes a plan naming the exact
state it was computed against: the target SHA, the SHA it expects to be active,
and the state revision. It changes nothing.

**Capture this plan and replay it.** Between planning and recording you will
install artefacts, restart services and run every gate, and any of those can
fail. Until they all pass, the authoritative state must keep naming the last
release that was actually validated. Planning again after validation would
describe whatever the state had become by then — including a promotion someone
else made in the meantime — and recording that would leave the machine running
one bundle while `slots.json` named another.

Install the bundle it named:

```sh
SOURCE=/var/lib/brain/runtime/bundles/previous
git checkout "$(basename "$(readlink -f "$SOURCE")")"
```

Swap the plugin without ever deleting the live copy first:

```sh
set -euo pipefail
PLUGINS=${PLUGINS:-/root/.hermes/plugins}
LIVE="$PLUGINS/brain-ceo-bridge"

# A residual .old is the fingerprint of an earlier swap that did not finish.
# Overwriting it would destroy the only copy of a plugin nobody restored.
if [ -e "$LIVE.old" ]; then
    echo "refusing: $LIVE.old exists from an earlier failed swap" >&2
    exit 1
fi

# Stage beside the live plugin, and guard every step that could leave a
# partial copy in place: a half-written bridge loads and fails at runtime
# rather than at install time.
rm -rf "$LIVE.new"
if ! cp -a "$SOURCE/plugin" "$LIVE.new"; then
    rm -rf "$LIVE.new"
    echo "copy failed; live plugin untouched" >&2
    exit 1
fi
if ! mv "$LIVE" "$LIVE.old"; then
    rm -rf "$LIVE.new"
    echo "could not set aside the live plugin; nothing changed" >&2
    exit 1
fi
if ! mv "$LIVE.new" "$LIVE"; then
    mv "$LIVE.old" "$LIVE"
    rm -rf "$LIVE.new"
    echo "swap failed; previous plugin restored" >&2
    exit 1
fi
rm -rf "$LIVE.old"
```

Then the config and the restart:

```sh
install -m 600 "$SOURCE/brain.toml" /etc/brain/brain.toml
systemctl restart brain.service
```

Restart the Hermes gateway, then run the **full validation block** above:
`hermes_integration_check.py`, `smoke_test.py`, `hermes_integrity.py`, and one
controlled CTWA. Only after every one of them passes:

```sh
PYTHONPATH=src .venv/bin/python scripts/brain_bundle.py record-rollback \
    --plan /var/lib/brain/runtime/staging/rollback-plan.json
```

`record-rollback` takes the exclusive state lock, re-reads `slots.json`, and
compares the revision and both SHAs against the captured plan. If anything
moved it refuses and records nothing, so the plan can only ever be applied to
the state it was made for. Otherwise it makes `previous` active and files the
outgoing bundle as the new `previous`, in one atomic write, so there is a way
back from the way back.

Recording earlier would leave the state naming a release nobody proved;
skipping it leaves `active` naming a deployment that no longer exists, and the
next operator to read it is told the wrong thing.

`active` and `previous` are one fact recorded in `slots.json`, not two
symlinks. The symlinks are a view redrawn from that file, so a stale or
tampered link changes nothing the tool believes. A slot that fails verification
is not a fallback, and `status` reports it as INVALID rather than letting that
be discovered mid-incident. Rollback never touches
`/usr/local/lib/hermes-agent`, which is not a deployment target.

### Architectural reversion

Returning to the pre-Amendment-2 code, the hooked plugin and a `turn_register`
principal reinstates the turn-correlation spine, the lifecycle writer and a
FamaChat credential in Brain. It requires explicit authorization and is never
an operator's own call during an incident.

If it is ever done, all three artefacts move together. Restoring the old plugin
under the new code, or the reverse, produces a system neither version was
tested as — and the specific shape of that failure is known: hooks calling a
`/internal/gateway/turn-register` route that no longer exists, failing open and
silently, which is how a whole turn was lost on 2026-08-31.
