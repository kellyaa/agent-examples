import json
import logging
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel

from autogen.beta import Agent
from autogen.beta.config import OpenAIConfig

from stateful_skill_demo.config.settings import Settings
from stateful_skill_demo.console_observer import ConsoleObserver
from stateful_skill_demo.schemas.execution_result import PlannerLog
from stateful_skill_demo.schemas.plan_step import PlanStep
from stateful_skill_demo.schemas.session import StepStatus

logger = logging.getLogger(__name__)

PLANNER_PROMPT = """\
You are a Planner agent. Given a user goal and the current session state,
produce exactly ONE next executable step.

Rules:
- Create only one step at a time.
- Each step must have a clear description and expected result.
- If prior steps failed, incorporate the failure reason and reviewer feedback.
- Never delete historical steps; if you need to replace a step, the orchestrator handles marking it REPLACED.
- When the goal is fully achieved, set goal_complete = true and step = null.
- When the goal cannot be achieved, set should_abort = true, provide abort_reason, and set step = null.

Output field requirements (every field MUST be present in every response):
- goal_complete: boolean
- should_abort: boolean
- abort_reason: string; use "" when not aborting
- step: object or null; null when goal_complete or should_abort is true

When step is an object, every field MUST be present:
- description: string
- inputs_json: string; use "{}" when the step needs no inputs
- expected_result: string; use "" if you have nothing meaningful to add
- expected_artifact_name: string or null; null when the step produces no named artifact

Step inputs:
- Provide step inputs as a JSON string in the `inputs_json` field (e.g. '{"path": "/tmp/foo"}').
- Use an empty object string '{}' if no inputs are needed.

Artifacts:
- A conversation can have named artifacts produced by prior successful steps.
- If a step produces a reusable output, set expected_artifact_name (e.g. "file_list").
- Reference a prior artifact inside inputs by writing "@{name}" as a string value;
  the executor will substitute its value before running the step.
  Example inputs_json: '{"content": "@{file_list}"}'
"""


class PlannerStep(BaseModel):
    model_config = {"json_schema_extra": {"additionalProperties": False}}

    description: str
    inputs_json: str
    expected_result: str
    expected_artifact_name: str | None


class PlannerOutput(BaseModel):
    model_config = {"json_schema_extra": {"additionalProperties": False}}

    goal_complete: bool
    should_abort: bool
    abort_reason: str
    step: PlannerStep | None


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
            response_schema=PlannerOutput,
            observers=[ConsoleObserver("planner")] if settings.verbose_agents else (),
        )

    async def plan(
        self,
        context_id: str,
        goal_id: str,
        user_goal: str,
        completed_steps: list[PlanStep],
        errors: list[dict] | None = None,
        feedback: str | None = None,
        prior_context: str = "",
        artifacts_block: str = "",
    ) -> tuple[PlanStep | None, PlannerLog, bool, bool, str]:
        """Returns (step_or_none, planner_log, goal_complete, should_abort, abort_reason)."""
        completed_summary = []
        for s in completed_steps:
            completed_summary.append({"step_id": s.id, "description": s.description, "status": s.status.value})

        prompt_parts = []
        if prior_context:
            prompt_parts.append(prior_context)
        if artifacts_block:
            prompt_parts.append(artifacts_block)
        prompt_parts.append(f"User goal: {user_goal}")
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
            "prior_context": prior_context,
            "artifacts_block": artifacts_block,
        }

        logger.info("Planner generating next step for context %s goal %s", context_id, goal_id)

        reply = await self._agent.ask(user_message)
        output = await reply.content(retries=1)
        if output is None:
            logger.error("Planner returned empty response for context %s goal %s", context_id, goal_id)
            output = PlannerOutput(
                goal_complete=False,
                should_abort=True,
                abort_reason="Planner returned an empty response.",
                step=None,
            )

        step_order = len(completed_steps) + 1
        step = None
        if output.step and not output.goal_complete and not output.should_abort:
            try:
                inputs = json.loads(output.step.inputs_json) if output.step.inputs_json else {}
                if not isinstance(inputs, dict):
                    inputs = {"value": inputs}
            except json.JSONDecodeError:
                logger.warning(
                    "Planner step.inputs_json was not valid JSON; treating as raw string: %s",
                    output.step.inputs_json[:200],
                )
                inputs = {"raw": output.step.inputs_json}

            step = PlanStep(
                id=f"step_{step_order}_{uuid.uuid4().hex[:8]}",
                context_id=context_id,
                goal_id=goal_id,
                step_order=step_order,
                description=output.step.description,
                inputs=inputs,
                expected_result=output.step.expected_result,
                expected_artifact_name=output.step.expected_artifact_name,
                status=StepStatus.PENDING,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

        planner_log = PlannerLog(
            id=f"plan_{uuid.uuid4().hex[:8]}",
            context_id=context_id,
            goal_id=goal_id,
            input_context=input_context,
            generated_plan=output.model_dump(),
            created_at=datetime.now(timezone.utc),
        )

        return step, planner_log, output.goal_complete, output.should_abort, output.abort_reason
