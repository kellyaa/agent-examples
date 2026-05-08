"""Console observer for AG2 beta agents.

Attached to each agent when ``Settings.verbose_agents`` is true. Subscribes
to the agent's conversation stream and prints every event to stdout with
the agent name, so Planner/Executor/Reviewer/Responder activity is visible
in the server terminal.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from autogen.beta.events import (
    BaseEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ClientToolCallEvent,
    BuiltinToolCallEvent,
    BuiltinToolResultEvent,
    HumanMessage,
)

logger = logging.getLogger(__name__)


def _short(text: str, limit: int = 500) -> str:
    text = text.replace("\n", " \\n ")
    if len(text) <= limit:
        return text
    return text[:limit] + f"... ({len(text) - limit} more chars)"


def _describe(event: BaseEvent) -> str:
    cls = type(event).__name__

    # ModelResponse carries the LLM's final message for the turn.
    if isinstance(event, ModelResponse):
        msg = getattr(event, "message", None)
        content = getattr(msg, "content", None) if msg else None
        if isinstance(content, list):
            parts = [p for p in content if isinstance(p, str)]
            content = " ".join(parts) if parts else repr(content)
        return f"{cls} :: {_short(str(content))}"

    if isinstance(event, ModelMessage):
        return f"{cls} :: {_short(str(getattr(event, 'content', '')))}"

    if isinstance(event, ModelRequest):
        msgs = getattr(event, "messages", None)
        n = len(msgs) if msgs is not None else "?"
        return f"{cls} :: {n} messages"

    if isinstance(event, (ClientToolCallEvent, BuiltinToolCallEvent)):
        name = getattr(event, "name", None) or getattr(event, "tool_name", None) or "?"
        args = getattr(event, "arguments", None) or getattr(event, "args", None)
        return f"{cls} :: {name}({_short(str(args), 200)})"

    if isinstance(event, BuiltinToolResultEvent):
        res = getattr(event, "result", None)
        return f"{cls} :: {_short(str(res), 400)}"

    if isinstance(event, HumanMessage):
        return f"{cls} :: {_short(str(getattr(event, 'content', '')))}"

    # Generic fallback: show a couple of interesting fields if present
    for field in ("content", "message", "name", "reason"):
        val = getattr(event, field, None)
        if val is not None:
            return f"{cls} :: {field}={_short(str(val))}"
    return cls


class ConsoleObserver:
    """AG2 beta observer that prints every stream event with the agent's name."""

    def __init__(self, agent_name: str) -> None:
        self._agent_name = agent_name

    def register(self, stack: contextlib.ExitStack, context: Any) -> None:
        def _on_event(event: BaseEvent) -> None:
            try:
                print(f"[{self._agent_name}] {_describe(event)}", flush=True)
            except Exception:  # never let logging break a run
                logger.exception("ConsoleObserver failed to render event")

        context.stream.subscribe(_on_event)
