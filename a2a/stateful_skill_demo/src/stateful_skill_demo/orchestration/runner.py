import json
import logging
import re
import uuid
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stateful_skill_demo.agents.executor import ExecutorAgent
from stateful_skill_demo.agents.planner import PlannerAgent
from stateful_skill_demo.agents.responder import ResponderAgent
from stateful_skill_demo.agents.reviewer import ReviewerAgent
from stateful_skill_demo.config.settings import Settings
from stateful_skill_demo.orchestration.context_builder import build_planner_context
from stateful_skill_demo.orchestration.resume import determine_resume_state
from stateful_skill_demo.persistence.session_store import ResumePoint, SessionStore
from stateful_skill_demo.schemas.artifact import Artifact, ArtifactKind
from stateful_skill_demo.schemas.execution_result import ExecutionResult, ReviewResult
from stateful_skill_demo.schemas.goal import Goal
from stateful_skill_demo.schemas.plan_step import PlanStep
from stateful_skill_demo.schemas.session import GoalStatus, ReviewStatus, StepStatus

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, bool], Coroutine[Any, Any, None]]

_ARTIFACT_REF = re.compile(r"@\{([a-zA-Z_][\w\-]*)\}")
_URL_RE = re.compile(r"^https?://\S+$")


class OrchestrationRunner:
    def __init__(
        self,
        settings: Settings,
        store: SessionStore,
        planner: PlannerAgent,
        executor: ExecutorAgent,
        reviewer: ReviewerAgent,
        responder: ResponderAgent,
    ):
        self._settings = settings
        self._store = store
        self._planner = planner
        self._executor = executor
        self._reviewer = reviewer
        self._responder = responder

    async def run_turn(
        self,
        context_id: str,
        turn_id: str,
        user_text: str,
        event_callback: EventCallback | None = None,
    ) -> str:
        """Handle one inbound user turn.

        The server has already inserted the user_turns row and the sessions row.
        This method resolves (or creates) the current goal, then drives the
        plan → execute → review loop for that goal.
        """

        emitted_final = False

        async def emit(msg: str, final: bool = False) -> None:
            nonlocal emitted_final
            if event_callback:
                await event_callback(msg, final)
            if final:
                emitted_final = True

        # 1. Resolve / create current goal.
        goal = await self._resolve_or_create_goal(context_id, turn_id, user_text, emit)

        # 2. Resume state for this goal (usually RUN_PLANNER for a fresh goal).
        resume = await determine_resume_state(context_id, self._store)

        # Safety: if resume still says AWAIT_NEW_GOAL after we just created one,
        # something's inconsistent. Treat as fresh plan.
        if resume.point == ResumePoint.AWAIT_NEW_GOAL or resume.goal is None or resume.goal.id != goal.id:
            resume_point = ResumePoint.RUN_PLANNER
            resume_current_step = None
            resume_last_execution = None
        else:
            resume_point = resume.point
            resume_current_step = resume.current_step
            resume_last_execution = resume.last_execution

        completed_steps = await self._store.get_steps_for_goal(goal.id)
        succeeded_steps = [s for s in completed_steps if s.status == StepStatus.SUCCEEDED]

        # 3. Mid-cycle resume handling.
        if resume_point == ResumePoint.RUN_EXECUTOR and resume_current_step:
            await emit(f"Resuming: executing step {resume_current_step.id}")
            _, review_result = await self._execute_and_review(resume_current_step, goal, emit)
            if review_result and review_result.goal_achieved:
                succeeded_steps.append(resume_current_step)

        elif resume_point == ResumePoint.RUN_REVIEWER and resume_current_step and resume_last_execution:
            await emit(f"Resuming: reviewing step {resume_current_step.id}")
            review_result = await self._run_review(resume_current_step, resume_last_execution)
            if review_result.goal_achieved:
                await self._store.update_step(resume_current_step.id, status=StepStatus.SUCCEEDED)
                succeeded_steps.append(resume_current_step)
                await self._maybe_create_artifact(resume_current_step, resume_last_execution, goal)
            else:
                await self._handle_failure(resume_current_step, review_result, goal)

        # 4. Main planning loop.
        while True:
            goal = await self._store.get_goal(goal.id)
            if not goal or goal.status != GoalStatus.ACTIVE:
                break

            if goal.failure_count >= self._settings.max_total_failures:
                await self._store.update_goal(goal.id, status=GoalStatus.FAILED)
                msg = "Goal failed: maximum total failures exceeded."
                await emit(msg, final=True)
                return msg

            errors = []
            feedback = None
            failed_steps = [s for s in completed_steps if s.status == StepStatus.FAILED]
            for fs in failed_steps[-3:]:
                last_exec = await self._store.get_latest_execution(context_id, fs.id)
                last_rev = await self._store.get_latest_review(context_id, fs.id)
                if last_exec and last_exec.error:
                    errors.append({"step": fs.id, "error": last_exec.error})
                if last_rev:
                    feedback = last_rev.recommended_action

            planner_ctx = await build_planner_context(
                self._store,
                context_id,
                goal.id,
                self._settings.planner_prior_goals_n,
                self._settings.planner_context_char_budget,
            )

            await emit("Planning next step...")
            step, planner_log, goal_complete, should_abort, abort_reason = await self._planner.plan(
                context_id=context_id,
                goal_id=goal.id,
                user_goal=goal.user_goal,
                completed_steps=succeeded_steps,
                errors=errors if errors else None,
                feedback=feedback,
                prior_context=planner_ctx.prior_context,
                artifacts_block=planner_ctx.artifacts_block,
            )
            await self._store.log_planner(planner_log)

            if goal_complete:
                await self._store.update_goal(goal.id, status=GoalStatus.COMPLETED)
                last_step = succeeded_steps[-1] if succeeded_steps else None
                if last_step:
                    last_exec = await self._store.get_latest_execution(context_id, last_step.id)
                    final_output = (
                        last_exec.summary if last_exec and last_exec.summary
                        else "Goal completed successfully."
                    )
                else:
                    # Zero-step completion — planner decided the prior context answers
                    # the goal directly. Ask the responder to produce a user-facing reply.
                    final_output = await self._responder.respond(
                        user_goal=goal.user_goal,
                        prior_context=planner_ctx.prior_context,
                        artifacts_block=planner_ctx.artifacts_block,
                    )
                await emit(final_output, final=True)
                return final_output

            if should_abort:
                await self._store.update_goal(goal.id, status=GoalStatus.FAILED)
                msg = f"Goal aborted: {abort_reason}"
                await emit(msg, final=True)
                return msg

            if not step:
                await self._store.update_goal(goal.id, status=GoalStatus.FAILED)
                msg = "Planner produced no step. Goal failed."
                await emit(msg, final=True)
                return msg

            await self._store.create_step(step)
            await self._store.update_goal(goal.id, current_step_id=step.id)
            completed_steps.append(step)

            await emit(f"Executing step: {step.description[:100]}")
            _, review_result = await self._execute_and_review(step, goal, emit)

            if review_result and review_result.goal_achieved:
                succeeded_steps.append(step)
            elif review_result:
                await self._handle_failure(step, review_result, goal)

        # Natural loop exit — goal state was mutated externally (e.g. SUPERSEDED).
        goal = await self._store.get_goal(goal.id) if goal else None
        final_status = goal.status.value if goal else "UNKNOWN"
        msg = f"Goal ended with status: {final_status}"
        if not emitted_final:
            await emit(msg, final=True)
        return msg

    # ── goal resolution ──

    async def _resolve_or_create_goal(
        self, context_id: str, turn_id: str, user_text: str, emit: EventCallback
    ) -> Goal:
        current = await self._store.get_current_goal(context_id)

        if current is None or current.status != GoalStatus.ACTIVE:
            # No goal, or current goal is terminal → this turn starts a new goal.
            goal = await self._store.create_goal(context_id, user_text, originating_turn_id=turn_id)
            if current is None:
                await emit(f"Starting goal #{goal.goal_index}.")
            else:
                await emit(
                    f"Previous goal #{current.goal_index} was {current.status.value}. "
                    f"Starting goal #{goal.goal_index}."
                )
            return goal

        # Current goal is ACTIVE — inbound turn supersedes it.
        superseded_ids = await self._store.supersede_active_goals(context_id, except_goal_id=None)
        if superseded_ids:
            logger.info("Superseded active goals: %s", superseded_ids)
        goal = await self._store.create_goal(context_id, user_text, originating_turn_id=turn_id)
        await emit(
            f"Superseded goal #{current.goal_index}. Starting goal #{goal.goal_index}."
        )
        return goal

    # ── step lifecycle ──

    async def _execute_and_review(
        self, step: PlanStep, goal: Goal, emit: EventCallback
    ) -> tuple[ExecutionResult, ReviewResult | None]:
        await self._store.update_step(step.id, status=StepStatus.IN_PROGRESS.value)

        resolved_step, missing = await self._resolve_artifact_refs(step)
        session_context = f"Working on: {resolved_step.description}"
        if missing:
            session_context += f" (note: unresolved artifact refs: {missing})"

        exec_result = await self._executor.execute(resolved_step, session_context)
        if missing:
            err = dict(exec_result.error or {})
            err["missing_artifacts"] = missing
            exec_result = exec_result.model_copy(update={"error": err})

        await self._store.log_execution(exec_result)

        await emit("Step executed. Reviewing results...")
        review_result = await self._run_review(resolved_step, exec_result)

        if review_result.goal_achieved:
            await self._store.update_step(step.id, status=StepStatus.SUCCEEDED.value)
            await self._maybe_create_artifact(resolved_step, exec_result, goal)
        else:
            await self._store.update_step(step.id, status=StepStatus.FAILED.value)

        return exec_result, review_result

    async def _run_review(self, step: PlanStep, exec_result: ExecutionResult) -> ReviewResult:
        review_result = await self._reviewer.review(step, exec_result)
        await self._store.log_review(review_result)
        logger.info(
            "Review for step %s: %s (achieved=%s)",
            step.id,
            review_result.review_status,
            review_result.goal_achieved,
        )
        return review_result

    async def _handle_failure(
        self, step: PlanStep, review_result: ReviewResult, goal: Goal
    ) -> None:
        new_retry = step.retry_count + 1
        await self._store.update_step(step.id, retry_count=new_retry)
        await self._store.update_goal(goal.id, failure_count=goal.failure_count + 1)

        if new_retry >= self._settings.max_step_retries:
            logger.warning("Step %s exceeded max retries (%d)", step.id, self._settings.max_step_retries)
            await self._store.update_step(step.id, status=StepStatus.FAILED.value)
        elif review_result.review_status == ReviewStatus.FAILURE:
            await self._store.update_step(step.id, status=StepStatus.FAILED.value)

    # ── artifact creation + substitution ──

    async def _resolve_artifact_refs(self, step: PlanStep) -> tuple[PlanStep, list[str]]:
        """Walk step.inputs; substitute @{name} tokens against the artifact registry.

        Returns (resolved_step, missing_names).
        Missing refs are left as the literal @{name} in the resolved inputs.
        """
        missing: list[str] = []

        async def resolve_str(s: str) -> str:
            # find all names in the string and substitute per-match
            names = _ARTIFACT_REF.findall(s)
            if not names:
                return s
            result = s
            for name in names:
                artifact = await self._store.get_artifact_by_name(step.context_id, name)
                if artifact is None:
                    if name not in missing:
                        missing.append(name)
                    continue
                if artifact.kind == ArtifactKind.FILE_PATH:
                    path = Path(artifact.value)
                    if not path.exists() and not artifact.is_stale:
                        await self._store.mark_artifact_stale(artifact.id)
                    result = result.replace(f"@{{{name}}}", artifact.value)
                else:
                    result = result.replace(f"@{{{name}}}", artifact.value)
            return result

        async def walk(value: Any) -> Any:
            if isinstance(value, str):
                return await resolve_str(value)
            if isinstance(value, dict):
                return {k: await walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [await walk(v) for v in value]
            return value

        resolved_inputs = await walk(step.inputs)
        resolved = step.model_copy(update={"inputs": resolved_inputs})
        return resolved, missing

    async def _maybe_create_artifact(
        self, step: PlanStep, exec_result: ExecutionResult, goal: Goal
    ) -> None:
        if not step.expected_artifact_name:
            return
        if exec_result.error:
            return
        if not exec_result.raw_output:
            return

        kind = _detect_artifact_kind(exec_result.raw_output, exec_result.artifact_kind_hint)
        artifact = Artifact(
            id=f"art_{uuid.uuid4().hex[:8]}",
            context_id=step.context_id,
            goal_id=goal.id,
            step_id=step.id,
            name=step.expected_artifact_name,
            kind=kind,
            value=exec_result.raw_output,
            summary=exec_result.summary or "",
            created_at=datetime.now(timezone.utc),
        )
        async with self._store._db.pool.acquire() as conn:
            async with conn.transaction():
                created = await self._store.create_artifact(artifact, conn=conn)
                await self._store.set_execution_artifact(exec_result.id, created.id, conn=conn)


def _detect_artifact_kind(raw: str, hint: str | None) -> ArtifactKind:
    if hint:
        try:
            return ArtifactKind(hint)
        except ValueError:
            pass
    stripped = raw.strip()
    if not stripped:
        return ArtifactKind.TEXT
    if _URL_RE.match(stripped):
        return ArtifactKind.URL
    if stripped[0] in "{[":
        try:
            json.loads(stripped)
            return ArtifactKind.JSON
        except Exception:
            pass
    # file_path heuristic: single-line that exists on disk
    if "\n" not in stripped and len(stripped) < 4096:
        try:
            if Path(stripped).exists():
                return ArtifactKind.FILE_PATH
        except OSError:
            pass
    return ArtifactKind.TEXT
