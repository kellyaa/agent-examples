import logging
import time
import uuid
from datetime import datetime, timezone

from autogen.beta import Agent
from autogen.beta.config import OpenAIConfig
from autogen.beta.tools import FilesystemToolkit, LocalShellTool, SkillSearchToolkit

from stateful_skill_demo.config.settings import Settings
from stateful_skill_demo.console_observer import ConsoleObserver
from stateful_skill_demo.schemas.execution_result import ExecutionResult
from stateful_skill_demo.schemas.plan_step import PlanStep

logger = logging.getLogger(__name__)

EXECUTOR_PROMPT = """\
You are an Executor agent. You receive a step to execute and use the available
tools (skills, filesystem, shell) to complete it.

Rules:
- Execute exactly the step described; do not do more or less.
- Use the available skills and tools to accomplish the task.
- Report what you did, what tool/skill you used, and whether it succeeded.
- If something fails, report the error clearly — do not retry on your own.
"""


class ExecutorAgent:
    def __init__(self, settings: Settings):
        self._settings = settings
        skills_dir = settings.skills_dir
        self._agent = Agent(
            "executor",
            EXECUTOR_PROMPT,
            config=OpenAIConfig(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_api_base,
                temperature=settings.llm_temperature,
            ),
            tools=[
                SkillSearchToolkit(skills_dir),
                FilesystemToolkit(skills_dir),
                LocalShellTool(skills_dir),
            ],
            observers=[ConsoleObserver("executor")] if settings.verbose_agents else (),
        )

    async def execute(self, step: PlanStep, session_context: str = "") -> ExecutionResult:
        prompt_parts = [
            f"Step: {step.description}",
        ]
        if step.inputs:
            prompt_parts.append(f"Inputs: {step.inputs}")
        if step.expected_result:
            prompt_parts.append(f"Expected result: {step.expected_result}")
        if session_context:
            prompt_parts.append(f"Session context: {session_context}")

        user_message = "\n".join(prompt_parts)
        logger.info("Executor running step %s: %s", step.id, step.description[:100])

        start = time.monotonic()
        error_dict = None
        raw_output = ""
        summary = ""
        skill_name = ""

        try:
            reply = await self._agent.ask(user_message)
            raw_output = await reply.content() or ""
            summary = raw_output[:500] if raw_output else "No output"
        except Exception as exc:
            logger.error("Executor failed on step %s: %s", step.id, exc)
            error_dict = {"type": type(exc).__name__, "message": str(exc)}
            summary = f"Execution failed: {exc}"

        runtime_ms = int((time.monotonic() - start) * 1000)

        return ExecutionResult(
            id=f"exec_{uuid.uuid4().hex[:8]}",
            context_id=step.context_id,
            goal_id=step.goal_id,
            step_id=step.id,
            skill_name=skill_name,
            skill_inputs=step.inputs,
            raw_output=raw_output,
            summary=summary,
            error=error_dict,
            runtime_ms=runtime_ms,
            created_at=datetime.now(timezone.utc),
        )
