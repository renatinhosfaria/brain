# brain-ceo-bridge

External Hermes plugin for the CEO Profile. It registers only the zero-argument
`conversation_context` tool in `brain-context`, plus the official
`pre_llm_call` and `pre_tool_call` hooks.

For each turn, `pre_llm_call` reads the current session through the public
`gateway.session_context.get_session_env()` API, requires the `default` Profile
and WhatsApp DM context, and registers the transient user message and opaque
turn ID with Brain. The raw message is never logged or stored by the plugin.

The tool then sends the five-field session reference plus Brain's registered
`wa_turn_id` to:

`http://127.0.0.1:8765/internal/gateway/conversation-context`

The `pre_tool_call` hook replaces model-provided idempotency for default-profile
WhatsApp DM `kanban_create` calls assigned to Porteiro, Cadastro, or Reno with
`whatsapp:<wa_turn_id>:<assignee>`. Other scopes are left unchanged.

The bridge token is supplied as the required `BRAIN_GATEWAY_TOKEN` secret. The
plugin never logs the token, context, response body, phone number, name, or
message. Missing current-turn proof, timeout, malformed response, or an
unavailable Brain result produces a controlled `status=unavailable` response.

Install a copy under Hermes' user plugin directory and enable it only for the
CEO Profile after Brain is configured. The source of truth for this plugin is
the versioned directory in the Brain repository; this repository does not edit
Hermes core files.
