"""Hermes plugin that bridges the CEO's current session to Brain."""

from .schemas import CONVERSATION_CONTEXT
from .tools import conversation_context, pre_llm_call, pre_tool_call


def register(ctx) -> None:
    ctx.register_tool(
        name="conversation_context",
        toolset="brain-context",
        schema=CONVERSATION_CONTEXT,
        handler=conversation_context,
        requires_env=["BRAIN_GATEWAY_TOKEN"],
        description=CONVERSATION_CONTEXT["description"],
    )
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
