import functools
import logging
import sys

from autogen import ConversableAgent
from autogen.mcp.mcp_client import Toolkit
from autogen.tools import Tool
from autogen.opentelemetry import instrument_agent
from opentelemetry.sdk.trace import TracerProvider

from k8s_debug_agent.config import Settings, settings
from k8s_debug_agent.llm import LLMConfig
from k8s_debug_agent.prompts import PLANNER_PROMPT, K8S_AGENT_PROMPT, GITHUB_AGENT_PROMPT

logger = logging.getLogger(__name__)
logging.basicConfig(level=settings.LOG_LEVEL, stream=sys.stdout, format="%(levelname)s: %(message)s")


def _truncate_toolkit(toolkit: Toolkit, max_chars: int) -> Toolkit:
    """Wrap each tool in a toolkit so its response is truncated to max_chars."""
    if max_chars <= 0:
        return toolkit

    wrapped_tools = []
    for tool in toolkit.tools:
        original_func = tool.func

        @functools.wraps(original_func)
        async def _capped_call(__orig=original_func, __name=tool.name, **kwargs):
            result = await __orig(**kwargs)
            if isinstance(result, tuple):
                text, meta = result
            else:
                text, meta = result, None

            if isinstance(text, str) and len(text) > max_chars:
                logger.warning(
                    "Truncating %s response from %d to %d chars",
                    __name, len(text), max_chars,
                )
                text = text[:max_chars] + f"\n\n... [TRUNCATED — response exceeded {max_chars} chars]"
            elif isinstance(text, list):
                total = sum(len(s) for s in text if isinstance(s, str))
                if total > max_chars:
                    logger.warning(
                        "Truncating %s response list from %d to %d chars",
                        __name, total, max_chars,
                    )
                    truncated = []
                    remaining = max_chars
                    for s in text:
                        if isinstance(s, str):
                            if len(s) <= remaining:
                                truncated.append(s)
                                remaining -= len(s)
                            else:
                                truncated.append(s[:remaining] + f"\n\n... [TRUNCATED]")
                                break
                        else:
                            truncated.append(s)
                    text = truncated

            return (text, meta) if meta is not None else text

        wrapped_tools.append(Tool(
            name=tool.name,
            description=tool.description,
            func_or_tool=_capped_call,
            parameters_json_schema=tool._func_schema["function"]["parameters"] if tool._func_schema else None,
        ))

    return Toolkit(tools=wrapped_tools)


class Agents:

    def __init__(
        self,
        config: Settings,
        k8s_toolkit: Toolkit | None = None,
        github_toolkit: Toolkit | None = None,
        tracer_provider: TracerProvider | None = None,
    ):
        llm_config = LLMConfig(config)

        self.planner = ConversableAgent(
            name="Planner",
            system_message=PLANNER_PROMPT.format(max_steps=config.MAX_PLAN_STEPS),
            llm_config=llm_config.planner_llm_config,
            code_execution_config=False,
            human_input_mode="NEVER",
        )

        self.k8s_agent = ConversableAgent(
            name="K8s_Agent",
            system_message=K8S_AGENT_PROMPT,
            llm_config=llm_config.k8s_agent_llm_config,
            code_execution_config=False,
            human_input_mode="NEVER",
        )

        self.github_agent = ConversableAgent(
            name="GitHub_Agent",
            system_message=GITHUB_AGENT_PROMPT,
            llm_config=llm_config.github_agent_llm_config,
            code_execution_config=False,
            human_input_mode="NEVER",
        )

        self.user_proxy = ConversableAgent(
            name="User",
            human_input_mode="NEVER",
            code_execution_config=False,
            is_termination_msg=lambda msg: (
                msg
                and "content" in msg
                and msg["content"] is not None
                and (
                    "##ANSWER" in msg["content"]
                    or "## Answer" in msg["content"]
                    or "##TERMINATE##" in msg["content"]
                    or ("tool_calls" not in msg and msg["content"] == "")
                )
            ),
        )

        if k8s_toolkit is not None:
            if config.MAX_TOOL_RESPONSE_CHARS > 0:
                k8s_toolkit = _truncate_toolkit(k8s_toolkit, config.MAX_TOOL_RESPONSE_CHARS)
            logger.info("Registering %d K8s MCP tools: %s", len(k8s_toolkit.tools), [t.name for t in k8s_toolkit.tools])
            k8s_toolkit.register_for_execution(self.user_proxy)
            k8s_toolkit.register_for_llm(self.k8s_agent)

        if github_toolkit is not None:
            if config.MAX_TOOL_RESPONSE_CHARS > 0:
                github_toolkit = _truncate_toolkit(github_toolkit, config.MAX_TOOL_RESPONSE_CHARS)
            logger.info("Registering %d GitHub MCP tools: %s", len(github_toolkit.tools), [t.name for t in github_toolkit.tools])
            github_toolkit.register_for_execution(self.user_proxy)
            github_toolkit.register_for_llm(self.github_agent)

        if tracer_provider is not None:
            instrument_agent(self.planner, tracer_provider=tracer_provider)
            instrument_agent(self.k8s_agent, tracer_provider=tracer_provider)
            instrument_agent(self.github_agent, tracer_provider=tracer_provider)
            instrument_agent(self.user_proxy, tracer_provider=tracer_provider)
