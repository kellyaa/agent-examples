import logging
from collections.abc import Callable, Coroutine
from typing import Any

from simple_skill_demo.agents.executor import ExecutorAgent
from simple_skill_demo.agents.planner import PlannerAgent
from simple_skill_demo.agents.reviewer import ReviewerAgent
from simple_skill_demo.config.settings import Settings
from simple_skill_demo.orchestration.resume import determine_resume_state
from simple_skill_demo.persistence.session_store import ResumePoint, SessionStore
from simple_skill_demo.schemas.session import ReviewStatus, SessionStatus, StepStatus

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, bool], Coroutine[Any, Any, None]]


class OrchestrationRunner:
    def __init__(
        self,
        settings: Settings,
        store: SessionStore,
        planner: PlannerAgent,
        executor: ExecutorAgent,
        reviewer: ReviewerAgent,
    ):
        self._settings = settings
        self._store = store
        self._planner = planner
        self._executor = executor
        self._reviewer = reviewer

    async def run_session(
        self,
        context_id: str,
        user_goal: str,
        event_callback: EventCallback | None = None,
    ) -> str:
        async def emit(msg: str, final: bool = False) -> None:
            if event_callback:
                await event_callback(msg, final)

        resume = await determine_resume_state(context_id, self._store)
        session = resume.session

        if resume.point == ResumePoint.COMPLETED:
            return "Session already completed."
        if resume.point == ResumePoint.FAILED:
            return "Session previously failed."

        if not session:
            session = await self._store.create_session(context_id, user_goal)
            await emit("Session created. Planning first step...")

        completed_steps = await self._store.get_steps(context_id)
        succeeded_steps = [s for s in completed_steps if s.status == StepStatus.SUCCEEDED]

        # Resume mid-cycle if needed
        if resume.point == ResumePoint.RUN_EXECUTOR and resume.current_step:
            await emit(f"Resuming: executing step {resume.current_step.id}")
            exec_result, review_result, resume = await self._execute_and_review(
                resume.current_step, context_id, emit
            )
            if review_result and review_result.goal_achieved:
                succeeded_steps.append(resume.current_step)

        elif resume.point == ResumePoint.RUN_REVIEWER and resume.current_step and resume.last_execution:
            await emit(f"Resuming: reviewing step {resume.current_step.id}")
            review_result = await self._run_review(resume.current_step, resume.last_execution, context_id)
            if review_result.goal_achieved:
                await self._store.update_step(resume.current_step.id, status=StepStatus.SUCCEEDED)
                succeeded_steps.append(resume.current_step)
            else:
                await self._handle_failure(resume.current_step, review_result, context_id, session)

        # Main planning loop
        while True:
            session = await self._store.get_session(context_id)
            if not session or session.status != SessionStatus.ACTIVE:
                break

            if session.failure_count >= self._settings.max_total_failures:
                await self._store.update_session(context_id, status=SessionStatus.FAILED.value)
                await emit("Session failed: maximum total failures exceeded.", final=True)
                return "Session failed: too many total failures."

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

            await emit("Planning next step...")
            step, planner_log, goal_complete, should_abort, abort_reason = await self._planner.plan(
                context_id=context_id,
                user_goal=user_goal,
                completed_steps=succeeded_steps,
                errors=errors if errors else None,
                feedback=feedback,
            )
            await self._store.log_planner(planner_log)

            if goal_complete:
                await self._store.update_session(context_id, status=SessionStatus.COMPLETED.value)
                await emit("Goal achieved! Session complete.", final=True)
                return "Goal completed successfully."

            if should_abort:
                await self._store.update_session(context_id, status=SessionStatus.FAILED.value)
                msg = f"Session aborted: {abort_reason}"
                await emit(msg, final=True)
                return msg

            if not step:
                await self._store.update_session(context_id, status=SessionStatus.FAILED.value)
                await emit("Planner produced no step. Session failed.", final=True)
                return "Planner produced no step."

            await self._store.create_step(step)
            await self._store.update_session(context_id, current_step_id=step.id)
            completed_steps.append(step)

            await emit(f"Executing step: {step.description[:100]}")
            exec_result, review_result, _ = await self._execute_and_review(step, context_id, emit)

            if review_result and review_result.goal_achieved:
                succeeded_steps.append(step)
            elif review_result:
                await self._handle_failure(step, review_result, context_id, session)

        session = await self._store.get_session(context_id)
        final_status = session.status if session else "UNKNOWN"
        return f"Session ended with status: {final_status}"

    async def _execute_and_review(self, step, context_id, emit):
        await self._store.update_step(step.id, status=StepStatus.IN_PROGRESS.value)

        session_context = f"Working on: {step.description}"
        exec_result = await self._executor.execute(step, session_context)
        await self._store.log_execution(exec_result)

        await emit("Step executed. Reviewing results...")
        review_result = await self._run_review(step, exec_result, context_id)

        if review_result.goal_achieved:
            await self._store.update_step(step.id, status=StepStatus.SUCCEEDED.value)
        else:
            await self._store.update_step(step.id, status=StepStatus.FAILED.value)

        resume = await determine_resume_state(context_id, self._store)
        return exec_result, review_result, resume

    async def _run_review(self, step, exec_result, context_id):
        review_result = await self._reviewer.review(step, exec_result)
        await self._store.log_review(review_result)
        logger.info(
            "Review for step %s: %s (achieved=%s)",
            step.id,
            review_result.review_status,
            review_result.goal_achieved,
        )
        return review_result

    async def _handle_failure(self, step, review_result, context_id, session):
        new_retry = step.retry_count + 1
        await self._store.update_step(step.id, retry_count=new_retry)
        await self._store.update_session(context_id, failure_count=session.failure_count + 1)

        if new_retry >= self._settings.max_step_retries:
            logger.warning("Step %s exceeded max retries (%d)", step.id, self._settings.max_step_retries)
            await self._store.update_step(step.id, status=StepStatus.FAILED.value)
        elif review_result.review_status == ReviewStatus.FAILURE:
            await self._store.update_step(step.id, status=StepStatus.FAILED.value)
