# brain-ceo-bridge

External Hermes plugin for the CEO Profile. It registers only the zero-argument
`conversation_phone` tool in `brain-context`.

For each call, the handler reads the current session through
`gateway.session_context.get_session_env()`, requires the `default` Profile and
WhatsApp DM context, and sends the five-field session reference to Brain at:

`http://127.0.0.1:8765/internal/gateway/conversation-phone`

The bridge token is supplied as the required `BRAIN_GATEWAY_TOKEN` secret. The
plugin never logs the token, context, response body, or phone number. Any
missing context, timeout, malformed response, or unavailable Brain result is
returned as `{"status":"unavailable","reason":"phone_not_resolved"}`.

Install a copy under Hermes' user plugin directory and enable it only for the
CEO Profile after Brain is configured. The source of truth for this plugin is
the versioned directory in the Brain repository; this repository does not edit
Hermes core files.
