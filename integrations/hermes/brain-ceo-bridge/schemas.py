"""Native Hermes schemas for the CEO Brain bridge."""

CONVERSATION_CONTEXT = {
    "name": "conversation_context",
    "description": "Return trusted transport context for the contact in this WhatsApp DM.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}
