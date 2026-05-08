import logging

from stateful_skill_demo.persistence.session_store import ResumePoint, SessionStore
from stateful_skill_demo.schemas.execution_result import ExecutionResult, ReviewResult
from stateful_skill_demo.schemas.goal import Goal
from stateful_skill_demo.schemas.plan_step import PlanStep

logger = logging.getLogger(__name__)


class ResumeState:
    def __init__(
        self,
        point: ResumePoint,
        goal: Goal | None = None,
        current_step: PlanStep | None = None,
        last_execution: ExecutionResult | None = None,
        last_review: ReviewResult | None = None,
    ):
        self.point = point
        self.goal = goal
        self.current_step = current_step
        self.last_execution = last_execution
        self.last_review = last_review


async def determine_resume_state(context_id: str, store: SessionStore) -> ResumeState:
    """Resolve resume-state scoped to the session's current goal.

    If no current goal or the current goal is terminal, returns AWAIT_NEW_GOAL —
    the runner interprets this as "create a new goal from the inbound turn."
    """
    point, goal = await store.get_resume_point_for_current_goal(context_id)

    current_step = None
    last_execution = None
    last_review = None

    if goal and goal.current_step_id:
        current_step = await store.get_step(goal.current_step_id)
        if current_step:
            last_execution = await store.get_latest_execution(context_id, current_step.id)
            if last_execution:
                last_review = await store.get_latest_review(context_id, current_step.id)

    logger.info(
        "Resume state for %s: %s (goal=%s, step=%s)",
        context_id,
        point,
        goal.id if goal else None,
        goal.current_step_id if goal else None,
    )

    return ResumeState(
        point=point,
        goal=goal,
        current_step=current_step,
        last_execution=last_execution,
        last_review=last_review,
    )
