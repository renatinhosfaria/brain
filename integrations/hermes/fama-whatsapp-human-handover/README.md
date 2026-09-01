# fama-whatsapp-human-handover

Durable, per-contact human handover for the CEO WhatsApp gateway.

When Hermes forwards an owner-authored WhatsApp message with
`whatsapp_from_owner=true`, the plugin pauses that contact, records the message
as an observed human assistant reply in the canonical Hermes transcript, and
interrupts any in-flight CEO turn. Customer messages received while paused are
recorded as observed user messages and skipped before the LLM.

Text and attachment type/path references are kept in the transcript. Media is
not transcribed while the contact is paused, because the silent path invokes no
LLM, tool or action. Any forwarded owner-looking message that exactly matches
a non-observed CEO response from the previous two minutes is treated as a
bridge echo and does not activate handover, including after a bridge-only
restart.

The pause remains until the configured CEO administrator sends
`/retomar <telefone>` in the configured Telegram chat and topic. Resuming does
not send a WhatsApp message or process the accumulated messages; the next
customer message follows the normal CEO flow with the complete transcript.

Required environment variables:

- `WHATSAPP_FORWARD_OWNER_MESSAGES=true`
- `FAMA_HANDOVER_TELEGRAM_CHAT_ID`
- `FAMA_HANDOVER_TELEGRAM_THREAD_ID`
- `FAMA_HANDOVER_TELEGRAM_USER_ID`

Optional:

- `FAMA_WHATSAPP_HANDOVER_DB` overrides the default durable store at
  `$HERMES_HOME/plugin-data/fama-whatsapp-human-handover/handover.db`.
