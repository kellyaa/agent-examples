import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from autogen.beta import Agent
from autogen.beta.config import OpenAIConfig

from stateful_skill_demo.config.settings import Settings
from stateful_skill_demo.console_observer import ConsoleObserver
from stateful_skill_demo.schemas.execution_result import ExecutionResult, ReviewResult
from stateful_skill_demo.schemas.plan_step import PlanStep
from stateful_skill_demo.schemas.session import ReviewStatus

logger = logging.getLogger(__name__)

REVIEWER_PROMPT = """\
You are a Reviewer agent. You evaluate whether an executor's output
successfully achieves the intended goal of a step.

Rules:
- Compare the step's expected result against the executor's actual output.
- Classify the result as SUCCESS, PARTIAL_SUCCESS, or FAILURE.
- If FAILURE or PARTIAL_SUCCESS, explain why and recommend a concrete action.

Output field requirements (every field MUST be present in every response):
- review_status: one of "SUCCESS", "PARTIAL_SUCCESS", "FAILURE"
- goal_achieved: boolean
- reason: string; use "" when there is nothing to explain
- recommended_action: string; use "" when no action is recommended
"""


class ReviewerOutput(BaseModel):
    model_config = {"json_schema_extra": {"additionalProperties": False}}

    review_status: Literal["SUCCESS", "PARTIAL_SUCCESS", "FAILURE"]
    goal_achieved: bool
    reason: str
    recommended_action: str


class ReviewerAgent:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._agent = Agent(
            "reviewer",
            REVIEWER_PROMPT,
            config=OpenAIConfig(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_api_base,
                temperature=settings.llm_temperature,
            ),
            response_schema=ReviewerOutput,
            observers=[ConsoleObserver("reviewer")] if settings.verbose_agents else (),
        )

    async def review(self, step: PlanStep, result: ExecutionResult) -> ReviewResult:
        prompt_parts = [
            f"Step description: {step.description}",
            f"Expected result: {step.expected_result}",
            f"Executor output summary: {result.summary}",
        ]
        if result.error:
            prompt_parts.append(f"Executor error: {result.error}")
        if result.raw_output and result.raw_output != result.summary:
            prompt_parts.append(f"Full executor output (truncated): {result.raw_output[:2000]}")

        user_message = "\n\n".join(prompt_parts)
        logger.info("Reviewer evaluating step %s", step.id)

        reply = await self._agent.ask(user_message)
        output = await reply.content(retries=1)
        if output is None:
            has_error = result.error is not None
            output = ReviewerOutput(
                review_status="FAILURE" if has_error else "SUCCESS",
                goal_achieved=not has_error,
                reason="Reviewer returned an empty response.",
                recommended_action="Retry the step" if has_error else "",
            )

        status = ReviewStatus(output.review_status)

        return ReviewResult(
            id=f"rev_{uuid.uuid4().hex[:8]}",
            context_id=step.context_id,
            goal_id=step.goal_id,
            step_id=step.id,
            review_status=status,
            goal_achieved=output.goal_achieved,
            reason=output.reason,
            recommended_action=output.recommended_action,
            created_at=datetime.now(timezone.utc),
        )
