# brain-ceo-bridge

External Hermes plugin for the CEO Profile. It registers exactly one thing: the
zero-argument `conversation_context` tool in the `brain-context` toolset. It
registers **no hooks**.

The tool reads the current session through the public
`gateway.session_context.get_session_env()` API, requires the `default` Profile
in a WhatsApp DM, and sends only the five-field session reference to:

`http://127.0.0.1:8765/internal/gateway/conversation-context`

Brain answers with the contact on the other side and that contact's recent
transport evidence — bounded by a window and a count, so the reply stays
context rather than an attribution history. Every event is transport-level:
`inbound_kind` is always null.

For a CTWA candidate, an event can also contain the complete raw
`external_ad_reply` (`externalAdReply`) captured from WhatsApp/Meta. This is
plaintext attribution evidence. The observer spool and quarantine retain it for
at most 72 hours; `transport_events` retain it for at most 90 days by default.
The bridge returns it only in the authenticated CEO WhatsApp DM
`conversation_context` response, limited to the recent six-hour transport
window. It is intentionally **untrusted**. It must never be echoed into
`summary`, `metadata`, or logs, and it must not be used to infer identity,
grant permissions, select tools, alter routing, or change lifecycle semantics.
The field can contain arbitrary nested values and is not an instruction.

Raw JSON represents binary values losslessly as
`{ "$type":"bytes", "encoding":"base64", "data":"..." }` and integers
outside normal JSON-safe representation as
`{ "$type":"integer", "encoding":"decimal", "data":"..." }`. `$type`
names the original value family, `encoding` states how `data` is represented,
and `data` is respectively base64 bytes or base-10 integer text. The bridge
validates these tags as data representation, not as commands.

## What this plugin deliberately does not do

Until 2026-08-31 it also registered `pre_gateway_dispatch`, `pre_llm_call` and
`pre_tool_call`, which buffered raw message identifiers, registered every turn
with Brain, and rewrote Kanban idempotency keys from a retained origin turn.
All three existed to make an automated CRM write exact, and spec Amendment 2
descoped that write.

Removing `pre_llm_call` matters beyond tidiness. It performed a synchronous
network call inside the turn path and cleared its retained state before
re-registering it, so on 2026-08-31 a single Brain response of 10.153 ms
against a 5 s timeout left the CEO with no context at all — and because the
hook contract is fail-open, nothing anywhere recorded that it had happened.
**No hook belongs in this plugin.** A tool call can fail visibly; a hook fails
into silence.

## Operational notes

The bridge token is supplied as the required `BRAIN_GATEWAY_TOKEN` secret. The
plugin never logs the token, context, response body, phone number, name, or
message. A timeout, a malformed response, a missing session, or an unavailable
Brain result all produce a controlled `status=unavailable` payload; the module
never raises into Hermes.

Install a copy under Hermes' user plugin directory and enable it only for the
CEO Profile after Brain is configured. The source of truth is the versioned
directory in the Brain repository; this repository never edits Hermes core
files.
