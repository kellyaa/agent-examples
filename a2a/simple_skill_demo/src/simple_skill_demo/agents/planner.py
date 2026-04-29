import json
import logging
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel

from autogen.beta import Agent
from autogen.beta.config import OpenAIConfig

from simple_skill_demo.config.settings import Settings
from simple_skill_demo.schemas.execution_result import PlannerLog
from simple_skill_demo.schemas.plan_step import PlanStep
from simple_skill_demo.schemas.session import StepStatus

logger = logging.getLogger(__name__)

PLANNER_PROMPT = """\
You are a Planner agent. Given a user goal and the current session state,
produce exactly ONE next executable step.

Rules:
- Create only one step at a time.
- Each step must have a clear description and expected result.
- If prior steps failed, incorporate the failure reason and reviewer feedback.
- Never delete historical steps; if you need to replace a step, the orchestrator handles marking it REPLACED.
- When the goal is fully achieved, respond with goal_complete = true and no step.
- When the goal cannot be achieved, respond with should_abort = true and a reason.

Respond ONLY with valid JSON matching this schema:
{
  "goal_complete": false,
  "should_abort": false,
  "abort_reason": "",
  "step": {
    "description": "what to do",
    "inputs": {},
    "expected_result": "what success looks like"
  }
}

If goal_complete is true, omit the step field or set it to null.
"""


class PlannerOutput(BaseModel):
    goal_complete: bool = False
    should_abort: bool = False
    abort_reason: str = ""
    step: dict | None = None


class PlannerAgent:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._agent = Agent(
            "planner",
            PLANNER_PROMPT,
            config=OpenAIConfig(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_api_base,
                temperature=settings.llm_temperature,
            ),
        )

    async def plan(
        self,
        context_id: str,
        user_goal: str,
        completed_steps: list[PlanStep],
        errors: list[dict] | None = None,
        feedback: str | None = None,
    ) -> tuple[PlanStep | None, PlannerLog, bool, bool, str]:
        """Returns (step_or_none, planner_log, goal_complete, should_abort, abort_reason)."""
        completed_summary = []
        for s in completed_steps:
            completed_summary.append({"step_id": s.id, "description": s.description, "status": s.status.value})

        prompt_parts = [f"User goal: {user_goal}"]
        if completed_summary:
            prompt_parts.append(f"Completed steps: {json.dumps(completed_summary)}")
        if errors:
            prompt_parts.append(f"Recent errors: {json.dumps(errors)}")
        if feedback:
            prompt_parts.append(f"Reviewer feedback: {feedback}")

        user_message = "\n\n".join(prompt_parts)
        input_context = {
            "user_goal": user_goal,
            "completed_steps": completed_summary,
            "errors": errors or [],
            "feedback": feedback or "",
        }

        logger.info("Planner generating next step for context %s", context_id)

        reply = await self._agent.ask(user_message)
        raw = await reply.content() or ""

        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
            output = PlannerOutput.model_validate_json(cleaned)
        except Exception:
            logger.warning("Planner output not valid JSON, treating as step description: %s", raw[:200])
            output = PlannerOutput(step={"description": raw, "inputs": {}, "expected_result": "Task completed"})

        step_order = len(completed_steps) + 1
        step = None
        if output.step and not output.goal_complete and not output.should_abort:
            step = PlanStep(
                id=f"step_{step_order}_{uuid.uuid4().hex[:8]}",
                context_id=context_id,
                step_order=step_order,
                description=output.step.get("description", ""),
                inputs=output.step.get("inputs", {}),
                expected_result=output.step.get("expected_result", ""),
                status=StepStatus.PENDING,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

        planner_log = PlannerLog(
            id=f"plan_{uuid.uuid4().hex[:8]}",
            context_id=context_id,
            input_context=input_context,
            generated_plan=output.model_dump(),
            created_at=datetime.now(timezone.utc),
        )

        return step, planner_log, output.goal_complete, output.should_abort, output.abort_reason
