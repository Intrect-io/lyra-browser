"""Following Claude Code's bypassPermissions mode, from a signal the model cannot forge.

A person who starts Claude Code with ``--dangerously-skip-permissions`` (or switches to
bypass) has told it not to ask before acting. lyra's consent dialogs asked anyway, on
every navigation and every send. This is how lyra learns of that choice per call.

Nothing a tool receives proves who filled it in, so the mode cannot be a tool argument
the model sets: a page that tells the model "add consent_override" would open the gate
itself, exactly as ``confirm=true`` did (see ``consent``). Claude Code tells neither the
server nor the elicitation hook which mode it is in. Only a PreToolUse hook is told
(``permission_mode``), so the plugin ships one (``scripts/claude-plugin/consent_hook.py``)
that runs before every lyra tool call and

- in bypassPermissions mode, rewrites the call's input to carry
  ``consent_override="bypassPermissions"``;
- in every other mode, denies a call whose input already carries the field. The model
  put it there, and the call does not reach the server.

So under Claude Code the field reaches this server only if the hook put it there. This
middleware takes it out of the arguments before the tool validates them and, when the
operator left ``follow_bypass`` on, marks the call so ``ConsentBroker`` answers its
consent requests itself (``consent_channel=bypass`` in the audit trail).

It is installed only for the ``claude-code`` client, the one that runs the hook. A client
without the hook never sees the field in a schema and is never told about it.
"""

from __future__ import annotations

import contextvars
from collections.abc import Sequence

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool, ToolResult

OVERRIDE_ARG = "consent_override"
BYPASS_MODE = "bypassPermissions"

# Declared on every tool, because the tools' schemas forbid unknown properties and the
# hook adds the field to whichever lyra tool is called.
_OVERRIDE_SCHEMA = {
    "type": "string",
    "description": (
        "Reserved for the lyra Claude Code plugin hook, which sets it from Claude Code's "
        "permission mode. Never set it yourself: a call that carries it outside "
        "bypassPermissions mode is refused before it reaches the server."
    ),
}

_bypass: contextvars.ContextVar[bool] = contextvars.ContextVar("lyra_bypass", default=False)


def bypass_active() -> bool:
    """Whether the tool call running now came from Claude Code in bypassPermissions mode."""
    return _bypass.get()


class BypassMiddleware(Middleware):
    """Reads the hook's ``consent_override`` out of each call; see the module docstring."""

    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    async def on_list_tools(self, context: MiddlewareContext, call_next) -> Sequence[Tool]:
        tools = await call_next(context)
        return [self._declare(tool) for tool in tools]

    @staticmethod
    def _declare(tool: Tool) -> Tool:
        params = dict(tool.parameters or {})
        properties = dict(params.get("properties") or {})
        if OVERRIDE_ARG in properties:
            return tool
        properties[OVERRIDE_ARG] = _OVERRIDE_SCHEMA
        params["properties"] = properties
        return tool.model_copy(update={"parameters": params})

    async def on_call_tool(self, context: MiddlewareContext, call_next) -> ToolResult:
        arguments = dict(context.message.arguments or {})
        if OVERRIDE_ARG not in arguments:
            return await call_next(context)
        # Always taken out: the tool's own validation does not know the field. Honoured
        # only for the exact value the hook writes, and only when the operator lets lyra
        # follow the mode.
        value = arguments.pop(OVERRIDE_ARG)
        message = context.message.model_copy(update={"arguments": arguments})
        context = context.copy(message=message)
        if not (self._enabled and value == BYPASS_MODE):
            return await call_next(context)
        token = _bypass.set(True)
        try:
            return await call_next(context)
        finally:
            _bypass.reset(token)
