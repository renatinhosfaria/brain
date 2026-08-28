"""Hermes plugin that bridges the CEO's current session to Brain."""

from .schemas import CONVERSATION_PHONE
from .tools import conversation_phone


def register(ctx) -> None:
    ctx.register_tool(
        name="conversation_phone",
        toolset="brain-context",
        schema=CONVERSATION_PHONE,
        handler=conversation_phone,
        requires_env=["BRAIN_GATEWAY_TOKEN"],
        description=CONVERSATION_PHONE["description"],
    )
