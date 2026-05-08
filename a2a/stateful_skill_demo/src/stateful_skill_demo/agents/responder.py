"""Responder agent — produces a direct user-facing answer when the planner
declares goal_complete without any steps (e.g. follow-up questions whose
answers live in prior-goal context)."""

import logging

from autogen.beta import Agent
from autogen.beta.config import OpenAIConfig

from stateful_skill_demo.config.settings import Settings
from stateful_skill_demo.console_observer import ConsoleObserver

logger = logging.getLogger(__name__)

RESPONDER_PROMPT = """\
You are a Responder agent. The Planner has determined that the user's current
goal can be answered directly from the prior conversation and available artifacts,
without running any tools or skills.

Your job: produce a concise, direct answer to the user's current goal, grounded
in the prior conversation context provided.

Rules:
- Answer in natural language, addressed to the user.
- Use information from the prior conversation and artifacts as needed.
- If the context does not contain enough information, say so briefly and suggest
  what the user could provide.
- Do NOT describe your reasoning, the planner, or mention that you are an AI.
- Do NOT emit JSON.
"""


class ResponderAgent:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._agent = Agent(
            "responder",
            RESPONDER_PROMPT,
            config=OpenAIConfig(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_api_base,
                temperature=settings.llm_temperature,
            ),
            observers=[ConsoleObserver("responder")] if settings.verbose_agents else (),
        )

    async def respond(
        self,
        user_goal: str,
        prior_context: str,
        artifacts_block: str,
    ) -> str:
        parts = []
        if prior_context:
            parts.append(prior_context)
        if artifacts_block:
            parts.append(artifacts_block)
        parts.append(f"Current user goal: {user_goal}")
        parts.append("Produce a direct answer to the user.")

        user_message = "\n\n".join(parts)
        logger.info("Responder generating direct answer for goal: %s", user_goal[:100])

        reply = await self._agent.ask(user_message)
        raw = await reply.content() or ""
        return raw.strip() or "Goal completed."
