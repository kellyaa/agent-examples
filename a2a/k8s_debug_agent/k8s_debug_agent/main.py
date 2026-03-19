import json
import logging
import sys

from autogen.mcp.mcp_client import Toolkit
from opentelemetry.sdk.trace import TracerProvider

from k8s_debug_agent.agents import Agents
from k8s_debug_agent.config import Settings, settings
from k8s_debug_agent.data_types import PlannerDecision
from k8s_debug_agent.event import Event

logger = logging.getLogger(__name__)
logging.basicConfig(level=settings.LOG_LEVEL, stream=sys.stdout, format="%(levelname)s: %(message)s")


class K8sDebugAgent:
    def __init__(
        self,
        config: Settings,
        eventer: Event | None = None,
        k8s_toolkit: Toolkit | None = None,
        github_toolkit: Toolkit | None = None,
        tracer_provider: TracerProvider | None = None,
    ):
        self.config = config
        self.eventer = eventer
        self.agents = Agents(config, k8s_toolkit=k8s_toolkit, github_toolkit=github_toolkit, tracer_provider=tracer_provider)
        self.findings: list[dict] = []

    async def _send_event(self, message: str, final: bool = False):
        logger.info(message)
        if self.eventer:
            await self.eventer.emit_event(message, final)

    def _extract_user_input(self, body: list[dict]) -> str:
        content = body[-1]["content"]
        if isinstance(content, str):
            return content
        latest = ""
        for item in content:
            if item["type"] == "text":
                latest += item["text"]
        return latest

    async def execute(self, user_messages: list[dict]) -> str:
        user_query = self._extract_user_input(user_messages)
        await self._send_event("Starting Kubernetes failure diagnosis...")

        gathered_info: list[str] = []
        remediation_actions: list[str] = []
        planner_context = f"User query: {user_query}"

        for step in range(self.config.MAX_PLAN_STEPS):
            await self._send_event(f"Planner step {step + 1}/{self.config.MAX_PLAN_STEPS}")

            # Ask planner for next decision
            decision = await self._get_planner_decision(planner_context)

            if decision.action == "final_report":
                await self._send_event("Generating final root-cause analysis report")
                return decision.instruction or "No report generated."

            if decision.action == "delegate_k8s":
                await self._send_event(f"Delegating to Kubernetes Agent: {decision.reasoning}")
                result = await self._delegate_to_agent(
                    self.agents.k8s_agent,
                    decision.instruction or "Inspect the cluster.",
                )
                source = "Kubernetes Agent"

            elif decision.action == "delegate_github":
                await self._send_event(f"Delegating to GitHub Agent: {decision.reasoning}")
                result = await self._delegate_to_agent(
                    self.agents.github_agent,
                    decision.instruction or "Retrieve repository information.",
                )
                source = "GitHub Agent"

            elif decision.action == "remediate_k8s":
                await self._send_event(f"Remediating via Kubernetes Agent: {decision.reasoning}")
                result = await self._delegate_to_agent(
                    self.agents.k8s_agent,
                    decision.instruction or "Attempt to fix the issue.",
                )
                source = "Kubernetes Agent (Remediation)"
                remediation_actions.append(
                    f"Action: {decision.instruction}\nResult: {result}"
                )

            else:
                logger.warning("Unknown planner action: %s", decision.action)
                continue

            gathered_info.append(f"[{source}] {result}")

            # Update planner context with all findings so far
            findings_text = "\n\n".join(gathered_info)
            remediation_text = "\n\n".join(remediation_actions) if remediation_actions else "None yet."
            planner_context = (
                f"User query: {user_query}\n\n"
                f"Information gathered so far:\n{findings_text}\n\n"
                f"Remediation actions taken so far:\n{remediation_text}\n\n"
                f"Decide the next step. If you have enough information, produce the final_report."
            )

        # Iteration limit reached -- force a final report
        await self._send_event("Iteration limit reached, producing best-effort report")
        findings_text = "\n\n".join(gathered_info)
        remediation_text = "\n\n".join(remediation_actions) if remediation_actions else "None."
        force_report_context = (
            f"User query: {user_query}\n\n"
            f"Information gathered:\n{findings_text}\n\n"
            f"Remediation actions taken:\n{remediation_text}\n\n"
            f"You have reached the maximum number of steps. "
            f"You MUST now produce the final_report with whatever information you have."
        )
        decision = await self._get_planner_decision(force_report_context)
        return decision.instruction or "Unable to produce a complete report within the step limit."

    async def _get_planner_decision(self, context: str) -> PlannerDecision:
        """Ask the planner agent for its next decision."""
        response = await self.agents.user_proxy.a_initiate_chat(
            message=context,
            recipient=self.agents.planner,
            max_turns=1,
        )
        raw_content = response.chat_history[-1]["content"]

        try:
            return PlannerDecision(**json.loads(raw_content))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse planner response as JSON: %s. Raw: %s", e, raw_content[:500])
            # Attempt to extract JSON from the response
            try:
                start = raw_content.index("{")
                end = raw_content.rindex("}") + 1
                return PlannerDecision(**json.loads(raw_content[start:end]))
            except (ValueError, json.JSONDecodeError):
                pass
            # Fall back to treating the whole response as a final report
            return PlannerDecision(
                action="final_report",
                instruction=raw_content,
                reasoning="Could not parse planner response; treating as final report.",
            )

    async def _delegate_to_agent(self, agent, instruction: str) -> str:
        """Delegate a task to a specialist agent and return the findings."""
        response = await self.agents.user_proxy.a_initiate_chat(
            message=instruction,
            recipient=agent,
            max_turns=5,
        )

        # Extract the agent's findings from chat history
        # Prefer tool responses for raw data, fall back to final message
        tool_data = ""
        for item in response.chat_history:
            if item.get("tool_responses"):
                for tool_response in item["tool_responses"]:
                    tool_data += tool_response.get("content", "") + "\n"

        final_message = response.chat_history[-1].get("content", "")

        if tool_data.strip():
            return f"Tool output:\n{tool_data}\n\nAgent summary:\n{final_message}"
        return final_message
