# CTWA Brain Lifecycle Architecture

**Date:** 2026-08-29  
**Amended:** 2026-08-30 (Amendment 1 — sections 3, 8, 10.1, 10.2, 10.3; see section 26)  
**Status:** Approved design, amended by production evidence; Plans 1 and 2 implemented  
**Primary repository:** `renatinhosfaria/brain`  
**Operational profile repository:** `renatinhosfaria/hermes`

## 1. Purpose

Build a production-grade path for Meta Ads Click-to-WhatsApp (CTWA) leads in which Brain captures transport attribution, provides trusted WhatsApp turn context to the Hermes CEO, and deterministically manages the CRM lifecycle:

- client creation starts at `Sem Atendimento`;
- the first successful Hermes/WhatsApp T1 send moves an eligible lead to `Não Respondeu`;
- the first genuine human inbound after the CTWA origin moves the lead to `Em Atendimento`;
- a human inbound that arrives before T1 skips `Não Respondeu` and goes directly to `Em Atendimento`.

The design must preserve the original Hermes Agent installation byte-for-byte. The solution is implemented in Brain plus the operational CEO/Profile files that are owned by the Fama deployment.

## 2. Hard invariants

### 2.1 Original Hermes Agent is immutable

Do not edit, patch, monkeypatch, preload, wrap, or replace any file distributed by NousResearch under the installed Hermes Agent tree, including but not limited to:

- `/usr/local/lib/hermes-agent/**`;
- `gateway/**`;
- official `plugins/**`;
- `scripts/whatsapp-bridge/**`;
- `bridge.js`;
- `delivery_ledger.py`;
- `kanban_tools.py`;
- `session_context.py`.

Using documented public extension interfaces such as `ctx.register_tool()` and `ctx.register_hook()` is allowed. The extension code itself must remain ours and live outside the upstream installation.

Before and after every deployment, verify the protected installation remains unchanged using the upstream Git HEAD/status plus hashes of critical WhatsApp/gateway files. Any unexpected change is a deployment failure.

### 2.2 Operational files may change

The following Fama-owned files may be changed for the CEO, Porteiro, Cadastro, and Reno Profiles:

- `SOUL.md`;
- `.hermes.md`;
- `config.yaml`;
- `profile.yaml`;
- local Profile skills owned by the Fama deployment when needed by the approved contracts.

### 2.3 Hermes databases are read-only to Brain

Brain may inspect Hermes `state.db` and `kanban.db` only through read-only connections. Existing protections (`mode=ro`, `PRAGMA query_only=ON`, and application-level write denial) remain mandatory.

### 2.4 FamaChat is authoritative for current commercial state

Brain may decide what lifecycle state is desired, but it must never assume that its cached state is newer than FamaChat. Any automated write must validate the live FamaChat record and must never downgrade a status changed by a human or another process.

### 2.5 LLMs do not own lifecycle transitions

CEO, Porteiro, Cadastro, and Reno may produce judgments and commercial content inside their scoped duties, but `Sem Atendimento -> Não Respondeu -> Em Atendimento` is a deterministic code path. No model is trusted to remember or execute those transitions directly.

## 3. Proven premises

The design is based on production evidence from 2026-08-29:

- installed Hermes Agent: `v0.20.6`, commit `c30ac90a92097058ddd6f9db3fa2e3182a7bfdcc`;
- installed Baileys: `@whiskeysockets/baileys 7.0.0-rc13`;
- a raw CTWA T0 was proven to arrive as `extendedTextMessage.contextInfo.externalAdReply`;
- a second, independently paired Baileys linked device remained connected alongside the Hermes device;
- the second device received the same tested historical CTWA signature;
- Hermes remained connected, queue length zero, with unchanged gateway/bridge PIDs and hashes;
- neither Brain nor Hermes repositories/configuration were changed by the spike.

The proven CTWA historical signature for the tested Meta Ads path is:

- `externalAdReply` present;
- `sourceType = ad`;
- `sourceApp = instagram` in the tested case;
- `sourceId` present;
- `sourceUrl` present and Instagram-hosted in the tested case;
- `ctwaClid` present;
- `showAdAttribution = true`;
- `clickToWhatsappCall = true`;
- `containsAutoReply = false`.

`containsAutoReply` must not be used to decide whether T0 is human. The tested CTWA T0 had `containsAutoReply=false`.

The newer conversion-family CTWA fields were absent in the tested event. V1 therefore treats the historical `externalAdReply` family as the proven semantic detector. New conversion-family fields may be captured as bounded observability metadata, but they must not create a production `ctwa_first_contact` classification until a controlled test proves their semantics.

### 3.1 Production evidence from 2026-08-30

These findings were produced on the live deployment after Plans 1 and 2 were implemented, the observer was paired as a second linked device, and the `brain-ceo-bridge` plugin was loaded. They amend sections 8 and 10; see section 26.

**P1 — Hermes and the observer see the same WhatsApp `key.id`.** `scripts/whatsapp-bridge/bridge.js` emits `messageId: msg.key.id`; the WhatsApp adapter copies it to `MessageEvent.message_id`; the observer derives its `event_id` from the same `msg.key.id`. Recomputing the observer's formula, `waevt_ + HMAC(transport_secret, observer_device_id || key.id)`, from the Hermes-side `message_id` produced event IDs **already present** in `transport_events` for 2 of 2 controlled messages. Both sides derive the same identifier independently, so exact-join correlation is available.

**P2 — `pre_gateway_dispatch` fires once per non-internal inbound message and skips internal events.** In one controlled run, three Kanban-completion notifications each registered a turn through `pre_llm_call` and produced **zero** `pre_gateway_dispatch` fires, while the single real inbound message fired it exactly once. This makes the hook a reliable discriminator between external WhatsApp turns and internal re-invocations.

**P3 — `pre_llm_call` receives `turn_id` in the installed version.** `agent/turn_context.py:1317-1329` passes `session_id`, `task_id`, `turn_id`, `user_message`, `conversation_history`, `is_first_turn`, `model`, `platform`, `parent_session_id`, `sender_id`. The public documentation omits `turn_id`; the installed source is authoritative for this deployment.

**P4 — Hook timeout coverage is an allowlist, and `pre_gateway_dispatch` is deliberately outside it.** `hermes_cli/plugins.py` places `pre_llm_call` in `_HOOK_TIMEOUT_BOUNDED_HOOKS`, where a timeout is fail-open ("abandon/skip, agent continues"), and excludes `pre_gateway_dispatch` because "abandoning is unsafe either way... prefer finish-or-exception fallthrough". A `pre_gateway_dispatch` callback therefore runs unbounded. **No network or blocking I/O may be performed in `pre_gateway_dispatch`**, or a slow Brain would wedge inbound message dispatch and violate section 20.

**P5 — Gateway and agent turn share one process.** `hermes-gateway.service` runs as a single process whose only child is the Node WhatsApp bridge; no agent subprocess is spawned. In-process state is therefore shared between `pre_gateway_dispatch`, `pre_llm_call`, and `pre_tool_call`.

**P6 — Body-HMAC plus time-window correlation is non-deterministic in production.** The same code path lost the race once (observer event and turn registration in the same second, result `pending`, no mapping) and won it once (event two seconds ahead, result `correlated`). Correlation computed only at registration time is never re-evaluated, so a lost race is permanent even when the matching event is already stored.

**P7 — Kanban cards for later stages carried distinct `wa_turn_id` values.** In one controlled lead, the Porteiro card carried the `wa_turn_id` of the real correlated WhatsApp turn, while the Cadastro and Reno cards carried `wa_turn_id` values belonging to Kanban-notification turns, which have no transport event and can never correlate. Section 10.3 binding is unreachable under the original rule.

**P8 — The amended contract was verified in production the same day.** Brain and the plugin were deployed in that order and one controlled WhatsApp DM was sent.

- The external turn correlated at registration, with its `turn_events` mapping persisted.
- All three stage cards carried one identical origin turn (`waturn_4b330613…` for Porteiro, Cadastro, and Reno). The immediately preceding lead, under the superseded rule, had shown `waturn_324d03f5…` for Cadastro and `waturn_81bd56b2…` for Reno.
- The two Kanban-completion re-invocations between those cards created **no** `whatsapp_turns` rows, where each previously produced a permanently pending one. This confirms `pre_gateway_dispatch` discriminates external turns from internal ones in production, not only in tests.
- The eight rows left pending by the superseded algorithm settled to `uncorrelatable` on their own, through re-evaluation finding no candidates. No dedicated migration was needed.

Two limits of this verification are recorded deliberately:

- **The P6 race was not exercised in that run.** The transport event was ingested about four seconds before the turn registered, so correlation succeeded at registration and the repair path never ran. Premise P9 later closed this.
- **Pending turns for a silent contact are not swept.** Re-evaluation runs only for the contact of an arriving event, so a turn whose contact never messages again keeps its `pending` status past the grace period. The periodic sweep belongs to the lifecycle reconciler, which does not exist yet.

`CONVERSATION_CONTEXT_E2E` remains open: it requires a real CTWA origin, and this verification used an ordinary DM.

**P9 — The re-evaluation repair path works in production.** In a later controlled DM the turn registered at 20:23:01 and the observer captured its event at 20:23:10, nine seconds behind. The turn still ended `correlated` with its mapping persisted, which is only reachable through re-evaluation on ingestion. The race that was permanent under the superseded algorithm is now recoverable, in production and not only in tests.

**P10 — The dispatch buffer key fails for a CTWA first contact.** A real Meta Ads click was captured correctly as `ctwa_candidate`, with `source_type=ad`, `source_app=instagram`, `www.instagram.com`, `sourceId` and `ctwaClid` present, `clickToWhatsappCall=true` and `containsAutoReply=false`, matching the section 3 signature. Correlation nevertheless failed: the `pre_gateway_dispatch` buffer was empty, both turns registered `uncorrelatable`, and the CEO never reached `conversation_context()`.

Ruled out by evidence: ordering (the event was stored three seconds before the turn), an unknown contact (the same contact correlated both eight minutes later and an hour earlier), and a stale plugin (the deployed file carried the current code).

The buffer is keyed on `(platform, chat_id)`, filled from `event.source.chat_id` in dispatch and drained by `HERMES_SESSION_CHAT_ID` at registration. The observer session shows `identity-key-<lid>_1.0.json` created nine seconds before the CTWA arrived, so that contact's LID identity was being established at that moment. The working hypothesis is that an ad-click first contact is addressed by phone JID at dispatch and by LID once the session resolves, so the two keys disagree. **This is not yet confirmed**; the fix must not be guessed.

Two consequences follow for the design:

- Keying the buffer on `chat_id` is fragile exactly where the project cannot afford it, at the first contact from an advertisement.
- Treating an empty buffer as terminal `uncorrelatable` turned a recoverable miss into a permanent verdict. That conflation, introduced to keep Kanban-notification turns from accumulating pending rows, needs a discriminator that does not depend on the buffer being reliable.

`RAW_CTWA_CAPTURE` is **PASS**. `CONVERSATION_CONTEXT_E2E` was **FAIL** here; premise P11 records it passing after the fix.

Separately and unrelated to this amendment, the Cadastro worker's `conversation_phone` fallback was denied with `AUTH_ORIGIN_AMBIGUOUS` during the same lead, which is what produced its degraded reply. That is pre-existing worker-origin authorization behaviour, not correlation.

**P11 — The cause of P10 was a stale buffered identifier, and the fixed path closes the Stage 3 gate.**

Root cause, reconstructed from stored evidence rather than instrumentation. A message at 19:18 was dispatched but produced no agent turn, so its identifier stayed in the buffer, which had inherited the turn map's one-hour TTL. Fifty-seven minutes later the CTWA turn drained both that orphan and its own identifier. Both resolved, both belonged to the right contact, but their joined length could not have produced the turn's message, so the turn was marked `uncorrelatable` — a terminal state. Every competing hypothesis was refuted by data first: ordering, an unknown contact, a stale plugin, and a `chat_id` addressing mismatch, the last by confirming both events belonged to the same contact and that the adapter passes `remoteJid` through unchanged.

Two defects, fixed independently. The buffer now expires on its own timescale (60 seconds, covering Hermes' debounce and far below an orphan's lifetime), and an inconsistent joined set resolves to `pending` rather than terminal, since the set is wrong rather than the turn.

A second controlled Meta Ads click then produced, end to end:

- `ctwa_candidate` captured with `source_app=instagram`;
- the turn `correlated`, with its `turn_events` mapping persisted to the CTWA event;
- `conversation_context()` reaching Brain and returning `allow`;
- Porteiro, Cadastro, and Reno cards all carrying one identical origin turn, with Porteiro and Cadastro reaching `done`.

**Stage 3 gates: `OBSERVER_COEXISTENCE`, `RAW_CTWA_CAPTURE`, `TURN_CORRELATION_CASES`, `KANBAN_IDEMPOTENCY`, and `CONVERSATION_CONTEXT_E2E` are all PASS**, on a lead originating from a real advertisement.

One unexplained observation is recorded rather than dismissed. An earlier CTWA turn registered `body_length=125` for a 62-character transport event, meaning Hermes saw 63 characters more than the observer. It suggests the platform prefixes ad context onto the message text in some cases. It did not occur in the passing run, but if it recurs the content consistency check will reject the joined set and the turn will sit `pending`. It needs its own controlled observation before section 8.3 can rely on that check.

## 4. Architecture

Production consists of three Brain-owned processes plus the unchanged Hermes runtime:

```text
                           WhatsApp
                              |
                 +------------+------------+
                 |                         |
                 v                         v
          Hermes Agent original     brain-whatsapp-observer
             UNMODIFIED                  .service
                 |                         |
                 |                         | allowlisted events
                 |                         v
                 |                   brain.service
                 |                    /        \
                 |                   /          \
            state.db RO       brain-runtime.db  kanban.db RO
                 |                   |
                 |                   v
                 |             Lifecycle Engine
                 |                   |
                 |                   v
                 |         brain-lifecycle-writer
                 |                 .service
                 |                   |
                 v                   v
                CEO               FamaChat
                 |
        Porteiro -> Cadastro -> Reno
```

### 4.1 `brain.service`

Brain owns:

- authenticated transport-event ingestion;
- WhatsApp turn correlation;
- `conversation_context()`;
- runtime persistence;
- Kanban reconciliation;
- Hermes delivery-ledger reconciliation;
- lifecycle fact derivation;
- lifecycle-effect creation and leasing;
- retention and audit metadata.

Brain does not possess the FamaChat credential that can change lifecycle status.

### 4.2 `brain-whatsapp-observer.service`

The observer owns a second WhatsApp linked-device session and only observes transport events. It must not send messages, explicitly mark messages read, send presence, react, mutate groups/contacts/profile, call FamaChat, create Kanban Tasks, or invoke an LLM.

Session path is independent of Hermes, for example:

`/var/lib/brain/whatsapp-observer/session/`

with directory mode `0700` and credential/evidence files `0600`.

Production Brain owns and pins its own Baileys dependency. The first supported version is the validated `@whiskeysockets/baileys 7.0.0-rc13`. The observer must not import, modify, or depend at runtime on the package tree under `/usr/local/lib/hermes-agent`; a future Hermes upgrade must not silently change the observer dependency. A new observer/Baileys version requires the compatibility tests in this spec before rollout.

The observer must never copy or reuse `/root/.hermes/platforms/whatsapp/session`.

### 4.3 `brain-lifecycle-writer.service`

The writer is a small deterministic service with no LLM. It is the only new component that holds the credential capable of applying the approved lifecycle status updates in FamaChat.

It does not decide transitions. It claims a precomputed effect from Brain, validates the live FamaChat client, executes only an allowlisted transition, performs readback, and reports the result to Brain.

## 5. Brain runtime persistence

Brain owns one writable SQLite database:

`/var/lib/brain/runtime/brain-runtime.db`

Only `brain.service` opens this database for writes.

Conceptual tables:

| Table | Responsibility |
| --- | --- |
| `transport_events` | Individual WhatsApp messages observed by the second device |
| `whatsapp_turns` | One Hermes CEO turn |
| `turn_events` | N:N mapping between a Hermes turn and transport messages |
| `kanban_bindings` | Porteiro/Cadastro/Reno Task identity tied to a WhatsApp turn |
| `lead_lifecycles` | CTWA-origin lifecycle tied to the exact created FamaChat client |
| `lifecycle_facts` | Durable objective facts such as client creation, T1 send success, human inbound |
| `lifecycle_effects` | Pending/applied/superseded/conflicted FamaChat effects |
| `contact_ephemera` | Short-lived WhatsApp display name needed for immediate registration |
| `reconcile_state` | Watermarks/checkpoints for Kanban and delivery-ledger scans |

SQLite uses WAL, bounded busy timeouts, migrations owned by Brain, foreign keys, unique constraints for all idempotency keys, and transactions around lifecycle fact/effect derivation.

## 6. Privacy and identifiers

### 6.1 `contact_key`

Raw phone numbers are not persisted in the Brain runtime database. Brain resolves the current verified phone from authorized WhatsApp identity evidence when needed and derives:

`contact_key = HMAC(secret, canonical_phone)`

The HMAC secret is a Brain secret and is never logged.

### 6.2 `event_id`

Each observer message gets a stable technical identifier:

`event_id = "waevt_" + HMAC(secret, observer_device_identity || observer_message_id)`

The raw observer `message_id` is accepted on the private ingestion boundary for deduplication but does not need to remain in long-term persistence after the stable `event_id` has been materialized.

Repeated Baileys `messages.upsert` deliveries for the same message produce the same `event_id` and are a no-op.

### 6.3 `wa_turn_id`

The public Hermes plugin hook `pre_llm_call` supplies an opaque Hermes `turn_id`. Our plugin registers the turn with Brain and derives:

`wa_turn_id = "waturn_" + HMAC(secret, hermes_turn_id)`

`wa_turn_id` identifies the CEO turn and is the source for Kanban idempotency. `event_id` identifies individual WhatsApp transport messages. They are intentionally different.

### 6.4 Stored transport data

Persist only bounded, allowlisted metadata:

- direction;
- timestamps;
- native message type;
- body length plus HMAC;
- contact key;
- CTWA classification;
- source type/app;
- source ID presence/length plus HMAC, not the raw source ID;
- source URL hostname, length plus HMAC, not the full URL;
- `ctwaClid` presence/length plus HMAC, not the raw value;
- boolean CTWA flags;
- key names/types for unproven future CTWA families when useful for diagnostics.

Do not persist raw message text, raw JID/LID, full `contextInfo`, full `externalAdReply`, full source URL, raw `sourceId`, raw `ctwaClid`, secrets, session keys, thumbnails, or opaque arbitrary payloads.

### 6.5 Display name

A WhatsApp display name may be retained in `contact_ephemera` for at most 24 hours so the CEO can propagate it to Cadastro. It is sanitized, length-bounded, and explicitly treated as untrusted WhatsApp profile data.

After 24 hours, delete the raw display name; only presence/HMAC metadata may remain with the transport record.

## 7. Transport classification

Transport classification and lifecycle-relative semantic classification are separate layers:

- `transport_kind` is transport-level evidence. Before a lifecycle exists, its bounded values are `ctwa_candidate` and `ordinary_inbound`.
- `inbound_kind` is lifecycle-relative semantic evidence. Before an exact lifecycle binding exists, it is `null`; transport evidence alone must not be called `ctwa_first_contact`, `human_inbound`, or `ctwa_attributed_inbound`.

No contact-global chronological rule is valid. In particular, Brain must never infer a lifecycle origin as the first or earliest CTWA event ever observed for a phone/contact, because that event may belong to a previous lifecycle or campaign.

### 7.1 Proven historical CTWA detector

A transport event may be classified as a CTWA attribution candidate when:

- `externalAdReply` is present;
- `sourceType == "ad"`;
- at least one strong CTWA signal is present: `clickToWhatsappCall == true`, `ctwaClid` present, or `sourceId` present.

`showAdAttribution` is supporting evidence, not a mandatory semantic identity field.

### 7.2 `ctwa_first_contact`

Lifecycle creation requires one exact event correlated to the Cadastro `wa_turn_id`, for the same verified contact and exact created client, with `transport_kind == ctwa_candidate`. It does not require a pre-existing `ctwa_first_contact` label.

The durable lifecycle binding itself establishes the semantic fact `lead_lifecycles.origin_event_id => ctwa_first_contact`. Until that binding exists, the event remains only a `ctwa_candidate` and its `inbound_kind` is `null`.

That event does **not** count as a human reply, even when `containsAutoReply=false`.

### 7.3 Later inbound classification

A later distinct event belonging to the same exact lifecycle is interpreted relative to its bound origin. An event after the origin with `transport_kind == ordinary_inbound` has `inbound_kind == human_inbound` and may create the human-inbound fact.

If a later distinct event in that lifecycle has `transport_kind == ctwa_candidate`, its `inbound_kind` is `ctwa_attributed_inbound` and it does not create a human-reply fact. This prevents a second ad-prefilled message from being mistaken for a manually typed response.

A duplicate delivery of an existing `event_id` never creates a human fact.

The approved fast-human edge case remains supported when the second manually typed message is an ordinary non-CTWA event:

```text
12:00:00 CTWA T0
12:00:02 manually typed ordinary message
```

The first event is CTWA origin; the second is human even if Hermes batches both into one CEO turn.

If future evidence shows that WhatsApp repeats the full CTWA signature on a manually typed second message, that ambiguous transport shape remains fail-closed until a controlled test produces a reliable discriminator. It must not be guessed as human.

## 8. Turn correlation

Hermes may debounce multiple WhatsApp messages into one turn, so correlation is between one `wa_turn_id` and one or more `event_id` values.

Correlation is by **exact message identifier**, not by content or timing. Premise P1 in section 3.1 proves that Hermes' `MessageEvent.message_id` and the observer's `msg.key.id` are the same value, so both sides derive the same `event_id` independently.

### 8.1 Collecting external message identifiers

The plugin registers `pre_gateway_dispatch`, which fires once per non-internal inbound message (premise P2) and carries `event.message_id`.

That callback **must perform no network call, no disk I/O, and no blocking work of any kind**. Premise P4 establishes that the hook runs unbounded: a slow dependency there would wedge inbound dispatch for every message and break section 20's requirement that Hermes keep serving when Brain is down. The callback appends the raw `message_id` to a bounded, TTL-expiring in-process buffer keyed by the current chat, and returns `None` so dispatch always proceeds.

Premise P5 establishes that this buffer is visible to `pre_llm_call` in the same process.

### 8.2 Registering the turn

`pre_llm_call` drains the buffer for the current chat and sends the accumulated `message_id` values together with the Hermes `turn_id` in one call to Brain's private registration route. All network I/O stays in this hook, which premise P4 shows is bounded and fail-open.

Raw `message_id` values cross the authenticated localhost boundary but are never persisted: Brain immediately derives `HMAC(transport_secret, ...)` and discards the raw values, exactly as it already treats the raw turn message body.

### 8.3 Proving the mapping

Brain correlates using only trusted facts:

1. authorize the current Hermes WhatsApp DM/session;
2. resolve the Hermes chat identity to one verified phone using the Hermes Baileys mapping directory;
3. independently resolve observer chat identity using the observer session mapping directory;
4. require both sides to resolve to the same canonical phone/contact key;
5. for each supplied `message_id`, derive the expected `event_id` using the observer's own formula and look it up directly;
6. require every derived `event_id` to resolve to a stored transport event for that same contact;
7. use the Hermes turn message content only transiently to recompute body HMAC/length as a **secondary consistency check** on the joined set; do not persist raw text;
8. persist the `turn_events` mapping, ordered by dispatch order, only after every identifier resolves.

An external turn whose buffer is empty, or any identifier that does not resolve to a stored event, is `turn_not_correlated`. Brain must not fall back to nearest-time or best-guess matching.

### 8.4 Re-evaluation and terminal states

Correlation must be re-evaluated rather than computed once. Premise P6 shows a registration-time-only computation is a coin flip: the observer's ingestion and Hermes' turn registration are independent pipelines with no ordering guarantee, and a lost race is otherwise permanent even when the matching event is already stored.

A turn therefore carries one of:

- `correlated` — every identifier resolved and the mapping is persisted; terminal;
- `pending` — at least one identifier has not yet been ingested; re-evaluated on later transport ingestion for that contact and on `conversation_context()`;
- `uncorrelatable` — terminal failure. A turn becomes `uncorrelatable` once its identifiers have not resolved within a bounded grace period well beyond normal observer ingestion latency, or when an identifier resolves to an event bound to a different contact.

`pending` must never be treated as a durable answer, and `uncorrelatable` must never be re-evaluated. `conversation_context()` reports `turn_not_correlated` for both.

Because internal events do not fire `pre_gateway_dispatch` (premise P2), a turn with an empty buffer is an internal re-invocation, not an external WhatsApp turn. Brain does not create a `whatsapp_turns` row for it; such turns produced permanent `pending` rows under the original rule.

The `ambiguous_transport_events` state remains defined for the `conversation_context()` contract, but exact-identifier correlation cannot produce it. It is retained for compatibility and for any future correlation path that reintroduces candidate selection.

Brain maintains an explicit Hermes-compatibility check for the state/Kanban schemas, required hook payloads, delivery-ledger semantics, and WhatsApp batching assumptions used by correlation. A Hermes upgrade may be installed normally, but lifecycle automation remains shadow/disabled until compatibility tests pass for the new version. Normal Hermes service must continue even when Brain declares the integration incompatible.

## 9. CEO `conversation_context()` contract

The CEO receives one zero-argument capability:

`conversation_context({})`

It replaces the CEO's need to call separate phone/event tools.

Success shape:

```yaml
status: ok
contact:
  phone_e164: "5534..."
  display_name: "Maria Silva"       # optional
  display_name_source: whatsapp_profile
turn:
  wa_turn_id: "waturn_..."
events:
  - event_id: "waevt_..."
    transport_kind: ctwa_candidate
    source_app: instagram
    inbound_kind: null
```

`transport_kind` is always present. `inbound_kind` is also always present but remains `null` before lifecycle binding. Once the lifecycle engine has an exact durable binding, the same stable shape may expose `ctwa_first_contact` for the bound origin, `human_inbound` for a later ordinary event, or `ctwa_attributed_inbound` for a later CTWA candidate. `conversation_context()` must not infer those meanings from contact chronology.

The response never exposes raw transport payloads, JIDs/LIDs, `ctwaClid`, full URLs, session credentials, or arbitrary `contextInfo`.

Fail-closed examples:

```yaml
status: unavailable
reason: contact_not_resolved
```

```yaml
status: unavailable
reason: turn_not_correlated
```

```yaml
status: unavailable
reason: ambiguous_transport_events
```

The CEO SOUL/skill must require `conversation_context()` before the first identity-dependent Kanban card for each external WhatsApp turn and must forbid fabrication of phone, `wa_turn_id`, or `event_id`.

If `conversation_context()` is unavailable, the lead must not be silenced. The CEO may create the minimum Porteiro task marked `context_resolution_failed`; the official `pre_tool_call` extension can still derive/enforce `wa_turn_id` idempotency from the Hermes hook turn when available. Porteiro/Cadastro may use their existing zero-argument `conversation_phone()` fallback for identity. Commercial routing may continue if identity is independently proven, but CTWA-origin lifecycle automation for that turn remains disabled until transport-event correlation is proven.

`conversation_phone()` remains available only as a fallback capability for Porteiro/Cadastro workers that receive an incomplete card.

## 10. Kanban idempotency and bindings

### 10.1 Idempotency format

All three Kanban cards for one lead use the **origin turn**, defined in 10.1.1:

- `whatsapp:<origin_wa_turn_id>:porteiro`;
- `whatsapp:<origin_wa_turn_id>:cadastro`;
- `whatsapp:<origin_wa_turn_id>:reno`.

`correlation_id` remains a separate UUID and never contains PII.

#### 10.1.1 Origin turn

The **origin turn** is the `wa_turn_id` of the external WhatsApp turn that started the lead — the turn whose `pre_gateway_dispatch` buffer carried the inbound message identifiers.

Porteiro, Cadastro, and Reno cards are created across several CEO invocations: the first from the customer's message, the later ones from Kanban-completion notifications. Premise P7 records that under the original rule each card took the `wa_turn_id` of whichever turn happened to be current, so the Cadastro card carried a notification turn that has no transport event and can never correlate. That made the section 10.3 binding unreachable in production.

All three stages must therefore share the **same** origin turn. An internal re-invocation never becomes an origin turn, and never replaces the current one.

### 10.2 Deterministic enforcement

Our Brain/Hermes extension registers a public `pre_tool_call` plugin hook. When the default CEO creates an approved Porteiro/Cadastro/Reno Kanban Task for a WhatsApp DM, the hook validates and, when necessary, replaces the model-supplied idempotency key with the correct value derived from the current **origin** `wa_turn_id`.

The plugin retains the origin turn per chat and refreshes it only on an external turn, identified by a non-empty `pre_gateway_dispatch` buffer (premise P2). A Kanban-notification turn leaves the retained origin untouched, so a Cadastro or Reno card created during such a turn still carries the originating WhatsApp turn.

The retained origin is bounded and TTL-expiring. When no origin turn is retained, the hook must leave the model-supplied key unchanged rather than substitute the current internal turn; a wrong binding is worse than an unrewritten key, which the reconciler can still discover.

The rewrite applies only to the approved assignees `porteiro`, `cadastro`, and `reno`. A card created under any other assignee label is left unchanged and does not participate in lifecycle binding.

The hook does not modify Hermes code. It uses the official plugin interface.

The durable source of truth is still `kanban.db`. Brain's reconciler discovers Tasks by idempotency key and records `kanban_bindings`. Hooks are a latency optimization, not the only source of truth.

### 10.3 Binding the created FamaChat client

Cadastro does not need a model-visible `lead_lifecycle_bind` tool.

After Cadastro completes, Brain uses the official Kanban completion hook as a fast path and then verifies the durable Task/run in `kanban.db` read-only. In the supported Hermes version, worker completion metadata is persisted on the run; Brain reads the terminal run's structured metadata rather than trusting a hook payload or parsing notification text when structured data is available.

Only a terminal Cadastro result with structured decision `LEAD_NOVO_CADASTRADO` and a single proven `client_id` can create a lifecycle binding.

Brain ties:

`origin CTWA event -> origin_wa_turn_id -> Cadastro Task -> exact client_id`.

The `wa_turn_id` in the Cadastro Task's idempotency key is the origin turn of section 10.1.1, which is the same turn the CTWA origin event correlates to. Binding must resolve the Cadastro Task through that origin turn and must never accept a Cadastro Task whose turn is an internal re-invocation with no correlated transport event.

A later attempt to bind the same origin to a different client is a hard conflict.

`JA_E_CLIENTE`, `CORRETOR_ATIVO`, `INCONCLUSIVO`, failed Cadastro readback, or any ambiguous terminal result must not create a new automated lifecycle.

## 11. Profile contracts

### 11.1 CEO

Update the operational CEO SOUL/skill/config so that external WhatsApp work:

- calls `conversation_context()` once for the turn before identity-dependent routing;
- uses only returned trusted phone/technical IDs;
- propagates `contact.display_name` to Cadastro when present, explicitly as untrusted WhatsApp profile data;
- propagates the minimum event facts needed by downstream workers;
- uses `wa_turn_id`-based idempotency;
- treats `ctwa_first_contact` as ad origin, not as human interest or reply;
- continues to deliver `metadata.response_ready` literally;
- follows the fail-open-for-service/fail-closed-for-lifecycle behavior defined in section 9 when Brain context is unavailable.

### 11.2 Porteiro

Business rule remains: any matching FamaChat `sistema_users` record with `isActive=true` is `CORRETOR_ATIVO`, regardless of role/department.

If the card lacks a verified phone, use zero-arg `conversation_phone()` as fallback. Otherwise use the card's Brain-proven phone.

### 11.3 Cadastro

The approved real-mode contract is:

1. search candidates with `fc_get_clientes` using the last four phone digits;
2. normalize and match locally;
3. any broker 35 record whose status is not `Arquivado` means `JA_E_CLIENTE`;
4. broker 35 archived, other-broker matches, or no match means `LEAD_NOVO`;
5. create one new record with `fc_post_clientes`, never alter the old record;
6. send exactly `phone`, `fullName`, `brokerId=35`, `source=Facebook Ads`; do not send `status`, `hasWhatsapp`, `whatsappJid`, or `profilePicUrl`;
7. `fullName` is the WhatsApp display name from the trusted card when available, otherwise `Lead WhatsApp <last4>`;
8. POST is attempted at most once;
9. read back the created record by exact ID with `fc_get_clientes_by_id` up to three total attempts: immediately, approximately +1s, approximately +2s;
10. success requires exact `id`, `brokerId=35`, and `status=Sem Atendimento`;
11. exhausted readback is `INCONCLUSIVO`; do not retry POST and do not send the flow to Reno.

### 11.4 Reno

On the first Reno turn following `LEAD_NOVO_CADASTRADO`, Reno must call exactly one `conversation_recent()` before producing its first commercial response.

Reno must not treat an ad click, CTWA attribution, or the CTWA first-contact message as proof of human interest. A later distinct non-CTWA human inbound may be used as genuine conversation evidence.

FamaChat structured state remains authoritative over historical Brain conversation content.

## 12. MCP least privilege

Use Hermes' supported `mcp_servers.<name>.tools.include` in the Fama-owned Profile configs.

### 12.1 Porteiro

Brain:

- `conversation_phone`

FamaChat:

- `fc_get_users`

No resources/prompts.

### 12.2 Cadastro

Brain:

- `conversation_phone`

FamaChat:

- `fc_get_clientes`;
- `fc_get_clientes_by_id`;
- `fc_post_clientes`.

No patch/delete/SQL tools. No resources/prompts.

### 12.3 Reno

Brain:

- `conversation_recent`;
- `conversation_search`.

FamaChat must use an explicit enumerated allowlist of the GET operations required for commercial service plus exactly these writes:

- `fc_post_clientes_by_id_notes`;
- `fc_post_appointments`.

Do not use a production wildcard such as `fc_get_*`. The implementation plan must enumerate the exact currently required GET tools from the Reno workflow and tests before changing the Profile config. Explicitly deny exposure of patch/put/delete/SQL tools by omission from `include`.

### 12.4 CEO

CEO's Brain context capability is the local `conversation_context()` plugin tool. CEO does not receive any FamaChat credential capable of lifecycle status updates.

## 13. Brain authentication and service principals

Extend Brain's own principal model with a `service` mode in addition to existing `gateway` and `worker` modes. This is a Brain change, not a Hermes change.

Required principals/capabilities:

- gateway/default: `conversation_context`;
- Porteiro worker: `conversation_phone`;
- Cadastro worker: `conversation_phone`;
- Reno worker: `conversation_recent`, `conversation_search`;
- observer service: `transport_ingest` only;
- lifecycle-writer service: `lifecycle_claim`, `lifecycle_result` only.

Each principal uses a distinct credential. Service principals do not accept model-supplied Kanban identity headers and do not inherit worker capabilities.

The FamaChat status-write credential exists only in `brain-lifecycle-writer.service`, never in a Profile, model-visible MCP configuration, observer, or `brain.service`.

## 14. Lifecycle facts and state machine

Brain persists facts rather than trusting transition order.

Minimum facts:

- `client_created_sem_atendimento`;
- `first_t1_send_success`;
- `first_human_inbound`.

When a lifecycle binding is first created, Brain immediately evaluates already-correlated transport events after the origin CTWA; a qualifying ordinary later event may therefore materialize `first_human_inbound` even if it arrived before Cadastro completed.

Desired state is derived:

```text
if first_human_inbound exists:
    Em Atendimento
else if first_t1_send_success exists:
    Não Respondeu
else:
    Sem Atendimento
```

This makes out-of-order events safe.

### 14.1 Approved scenarios

**Normal:**

`CTWA -> client created -> T1 success -> human reply`

`Sem Atendimento -> Não Respondeu -> Em Atendimento`

**Human before T1:**

`CTWA -> ordinary human reply -> client created/T1`

The lifecycle becomes `Em Atendimento` directly. A later T1 success does not downgrade it.

**CTWA + human inside Hermes debounce:**

One `wa_turn_id` maps to two `event_id` values. The CTWA event creates the origin; a second qualifying ordinary event creates `first_human_inbound`, so the desired state is `Em Atendimento`.

**Second CTWA-attributed message:**

A later event that itself matches the proven CTWA detector does not create `first_human_inbound`; it remains attribution evidence only unless a future controlled test establishes a reliable human discriminator.

## 15. Proving first T1 send success

In the installed Hermes commit, `delivery_obligations.state='delivered'` means Hermes marked the final outbound after `SendResult.success`. For WhatsApp, that is the approved business trigger: successful send through the Hermes/Baileys transport path. It does not mean two WhatsApp ticks or message read.

Brain reads `delivery_obligations` from Hermes `state.db` read-only.

To prove the first Reno T1 for a lifecycle, Brain requires a unique match using:

- the same authorized Hermes `session_key`;
- final Reno `metadata.response_ready` content, HMAC-compared against ledger `content` without persisting the raw response in Brain runtime;
- a compatible time window after the Reno Task/CEO turn;
- `state='delivered'`.

Exactly one match creates `first_t1_send_success`. Zero or multiple indistinguishable matches are `NOT_PROVEN` and must not move the CRM to `Não Respondeu`.

Hermes retains delivery obligations for a bounded period (approximately seven days in the tested version), so Brain's reconciler must capture the durable lifecycle fact promptly. If Brain is unavailable beyond the Hermes retention window and proof disappears, do not infer delivery; alert and leave the lifecycle unchanged.

## 16. Lifecycle effects and writer contract

Brain creates an effect only when the desired lifecycle state differs from the last FamaChat state that has been proven.

Allowed automatic transitions are exactly:

- `Sem Atendimento -> Não Respondeu`;
- `Sem Atendimento -> Em Atendimento`;
- `Não Respondeu -> Em Atendimento`.

No other status transition is authorized.

Effect states:

- `pending`;
- `claimed`;
- `applied`;
- `already_applied`;
- `superseded`;
- `conflict`;
- `retryable`;
- `permanent_failure`.

If a newer fact makes a pending effect obsolete, mark it `superseded` before the writer can claim it.

The writer workflow is:

1. claim one leased effect from Brain;
2. GET the exact FamaChat client;
3. prove `client_id`, `brokerId=35`, lifecycle/contact binding, and current status;
4. if current status already equals target, report `already_applied` and do not PATCH;
5. if current status differs from the expected source state, report `conflict` and do not PATCH;
6. execute only the allowlisted status mutation;
7. GET/read back the same client;
8. require target status exactly;
9. report the durable result to Brain.

A writer crash after PATCH but before reporting is safe: after lease expiry, the next writer GET sees the target status and returns `already_applied`.

## 17. Conditional-write production gate

A GET-then-PATCH sequence alone is not considered atomic enough to protect against a human changing the FamaChat status between the GET and PATCH.

Before `BRAIN_LIFECYCLE_WRITE_ENABLED=true`, a dedicated FamaChat capability spike must prove one of:

- a compare-and-set status endpoint;
- a version/ETag/If-Match mechanism;
- another atomic conditional update whose server-side predicate includes the expected current status.

If no such mechanism exists, automated lifecycle writes remain disabled. The implementation may run in shadow/dry-run mode indefinitely until an atomic server-side protection is available. Do not silently substitute ordinary GET+PATCH as if it were equivalent.

## 18. Observer outbox and recovery

The observer may keep a local technical outbox separate from Brain's primary runtime database, for example:

`/var/lib/brain/whatsapp-observer/outbox.db`

Flow:

1. receive `messages.upsert`;
2. normalize to the allowlisted event shape;
3. durably append to local outbox;
4. POST to Brain's authenticated localhost ingestion endpoint;
5. remove/acknowledge the outbox item only after Brain confirms durable ingestion.

If Brain restarts, the observer retains and retransmits pending events. Brain deduplicates by `event_id`.

The outbox uses bounded retention and contains no raw message text or raw transport payload.

## 19. Retention

- raw WhatsApp display name: maximum 24 hours;
- transport events, turn/event mappings, CTWA technical attribution: maximum 90 days;
- minimal active lifecycle binding (`contact_key`, `client_id`, lifecycle ID, phase): retained while the lifecycle remains active;
- after the lifecycle becomes terminal for this automation (`Em Atendimento` or leaves the managed statuses), retain minimal lifecycle/effect audit for 90 additional days, then purge;
- secrets and Baileys credentials follow service-secret/session lifecycle, never general event retention.

The purpose of retaining the minimal active binding beyond 90 transport days is to support a late human reply without retaining historical raw CTWA data.

## 20. Failure behavior

### Observer down

Hermes continues normal WhatsApp service. Brain marks observer health degraded. No transport-origin classification is guessed while evidence is missing.

### Brain down

Hermes continues normal service. Observer buffers bounded allowlisted events. Writer cannot claim new effects.

### Writer down

Hermes and Brain continue. Effects remain pending and resume after writer recovery.

### FamaChat down

Writer reports retryable failures; no effect is treated as applied without readback.

### Plugin hook failure

Official plugin hooks are best-effort. Durable truth is reconstructed from `state.db`, `kanban.db`, Brain runtime data, and observer outbox. Hooks improve latency but are not the sole persistence path.

### Ambiguous identity/correlation

Fail closed. Do not create lifecycle facts/effects from probabilistic matches.

### Unsupported Hermes compatibility

Hermes continues serving normally. Brain context/lifecycle capabilities return controlled unavailable/degraded states, and lifecycle writes remain disabled until the compatibility suite passes for the new Hermes version/schema/semantics.

## 21. Health, audit, and metrics

Expose health without PII:

- Brain runtime DB status;
- read-only Hermes state/Kanban access;
- Hermes compatibility status;
- observer connection state;
- observer outbox depth/oldest age;
- lifecycle mode (`shadow`, `dry_run`, `write`);
- pending effects and oldest pending age;
- FamaChat writer connectivity.

Required alerts include:

- observer disconnected beyond a defined operational threshold;
- growing/outdated outbox;
- `turn_not_correlated` or ambiguous correlation;
- lifecycle missing a client binding after expected completion;
- delivery proof not captured before retention risk;
- effects stuck retryable;
- write readback mismatch;
- attempted downgrade/conflict;
- unsupported Hermes compatibility;
- protected Hermes installation integrity changed.

Metrics must not use phone, name, message text, client ID, or other PII as labels.

## 22. Rollout

### Phase A: production capture, no CRM writes

Deploy Brain runtime, observer, `conversation_context()`, profile contracts, and correlation. `BRAIN_LIFECYCLE_WRITE_ENABLED=false`.

### Phase B: controlled shadow E2E

Prove at minimum:

- CTWA-only T0 remains `Sem Atendimento` until T1;
- successful T1 produces desired `Não Respondeu` in shadow;
- ordinary human after T1 produces desired `Em Atendimento`;
- ordinary human before T1 skips `Não Respondeu`;
- CTWA + ordinary human inside one debounce turn produces two events and desired `Em Atendimento`;
- a second CTWA-attributed event is not falsely counted as human;
- `JA_E_CLIENTE` creates no new lifecycle;
- `CORRETOR_ATIVO` creates no lifecycle;
- failed Cadastro readback creates no active lifecycle/Reno handoff;
- failed Hermes send creates no T1-success fact;
- restart/reconnect does not duplicate events/effects;
- manual CRM state changes are never downgraded;
- unsupported Hermes compatibility disables lifecycle automation without harming normal Hermes service.

All deterministic cases must pass before writes are considered.

### Phase C: FamaChat conditional-write gate

Prove an atomic expected-status write primitive. Failure leaves write mode disabled.

### Phase D: writer dry-run

Run the real writer claim/GET/validation path but report `would_apply`; no PATCH.

### Phase E: restricted write activation

Enable writes only for newly observed lifecycles that are:

- proven CTWA-origin;
- created by the approved Cadastro flow;
- exact bound client;
- `brokerId=35`;
- `source=Facebook Ads`;
- in one of the managed source statuses.

Do not backfill or mass-update historical clients.

## 23. Go-live gates

All must be `PASS` before lifecycle writes are enabled:

- `OBSERVER_COEXISTENCE` — already proven;
- `RAW_CTWA_CAPTURE` — already proven;
- `CONVERSATION_CONTEXT_E2E`;
- `TURN_CORRELATION_CASES`;
- `KANBAN_IDEMPOTENCY`;
- `CADASTRO_READBACK`;
- `RENO_FIRST_HISTORY`;
- `LIFECYCLE_SHADOW`;
- `RESTART_RECOVERY`;
- `FAMACHAT_CONDITIONAL_WRITE`;
- `HERMES_COMPATIBILITY`;
- `HERMES_ORIGINAL_INTEGRITY`.

A gate in `FAIL` or `NOT_PROVEN` keeps `BRAIN_LIFECYCLE_WRITE_ENABLED=false`.

## 24. Non-goals

This project does not:

- modify upstream Hermes Agent source or configuration defaults;
- replace the Hermes WhatsApp device/session;
- make the Brain observer send WhatsApp messages;
- infer CTWA from message text alone;
- treat T0 or any later independently CTWA-attributed event as a human reply;
- wait for WhatsApp two-tick/read receipts before `Não Respondeu`;
- give Reno/CEO direct authority to change lifecycle status;
- reactivate or mutate archived/other-broker records during Cadastro;
- bulk-correct historical CRM records;
- persist raw WhatsApp conversation text in Brain's transport database.

## 25. Acceptance summary

The design is complete when a new Meta CTWA lead can be traced deterministically through:

```text
raw observer event
  -> event_id/contact_key
  -> correlated wa_turn_id
  -> conversation_context()
  -> Porteiro
  -> Cadastro exact client creation/readback
  -> lifecycle binding
  -> Reno first-turn history + response_ready
  -> Hermes delivery_obligation delivered
  -> first_t1_send_success fact
  -> desired Não Respondeu
  -> later distinct proven ordinary human inbound
  -> desired Em Atendimento
  -> atomic, idempotent FamaChat effect
```

while the original Hermes Agent installation remains unchanged and every ambiguous or unproven correlation fails closed.

## 26. Amendment log

### Amendment 1 — 2026-08-30

Cause: production evidence gathered after Plans 1 and 2 were deployed and the observer was paired. Recorded as premises P1 through P7 in section 3.1.

| Section | Change |
| --- | --- |
| 3.1 | New. Records premises P1–P7. |
| 8 | Correlation is by exact `message_id` join instead of body-HMAC plus debounce window. Adds 8.1 (identifier collection, no I/O in `pre_gateway_dispatch`), 8.2 (registration), 8.3 (proof), 8.4 (re-evaluation and the new terminal `uncorrelatable` state). |
| 10.1 | All three stage cards must use the shared **origin turn**, newly defined in 10.1.1. |
| 10.2 | `pre_tool_call` derives the key from the retained origin turn, refreshed only on external turns; leaves keys unchanged when no origin is retained or the assignee is outside the approved set. |
| 10.3 | Binding resolves the Cadastro Task through the origin turn and rejects internal re-invocations. |

What did **not** change: the fail-closed principle, the privacy model, the CTWA detector of 7.1, the `conversation_context()` response contract of section 9, the lifecycle state machine of section 14, and every constraint in section 2.

Two defects motivated the amendment. Correlation computed only at registration time was a race whose outcome varied run to run (P6), and the stage cards carried unrelated `wa_turn_id` values that made the section 10.3 binding unreachable (P7). Both are fixed by one mechanism: `pre_gateway_dispatch` fires only for external messages, so its buffer both supplies exact identifiers and distinguishes an external turn from an internal re-invocation.

Downstream documents requiring updates before implementation:

- Plan 1 (`2026-08-29-ctwa-brain-transport-context.md`), Task 4 and Task 5 — hook contract, registration payload, correlation algorithm, `uncorrelatable` state.
- Plan 4 (`2026-08-29-ctwa-lifecycle-engine.md`), Task 2 — origin-turn resolution in `parse_whatsapp_idempotency_key` and `bind_completed_cadastro`.

Implementation status: the amendment was implemented and deployed on 2026-08-30, and premise P8 records the production verification. The disposable `ctwa-keyid-spike` plugin used to produce P1 and P2 has been removed. Plan 4 is unchanged in code and still describes the binding this amendment makes reachable.
