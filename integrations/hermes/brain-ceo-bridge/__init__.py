"""Hermes plugin that bridges the CEO's current session to Brain."""

from .schemas import CONVERSATION_CONTEXT
from .tools import conversation_context


def register(ctx) -> None:
    # Amendment 2: this plugin registers no hooks. Turn registration, the
    # dispatch identifier buffer and Kanban idempotency rewriting all existed
    # to support an automated CRM write that Brain no longer performs.
    ctx.register_tool(
        name="conversation_context",
        toolset="brain-context",
        schema=CONVERSATION_CONTEXT,
        handler=conversation_context,
        requires_env=["BRAIN_GATEWAY_TOKEN"],
        description=CONVERSATION_CONTEXT["description"],
    )
