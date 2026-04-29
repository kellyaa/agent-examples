import logging

from simple_skill_demo.persistence.session_store import ResumePoint, SessionStore
from simple_skill_demo.schemas.execution_result import ExecutionResult, ReviewResult
from simple_skill_demo.schemas.plan_step import PlanStep
from simple_skill_demo.schemas.session import Session

logger = logging.getLogger(__name__)


class ResumeState:
    def __init__(
        self,
        point: ResumePoint,
        session: Session | None = None,
        current_step: PlanStep | None = None,
        last_execution: ExecutionResult | None = None,
        last_review: ReviewResult | None = None,
    ):
        self.point = point
        self.session = session
        self.current_step = current_step
        self.last_execution = last_execution
        self.last_review = last_review


async def determine_resume_state(context_id: str, store: SessionStore) -> ResumeState:
    session = await store.get_session(context_id)
    if not session:
        return ResumeState(point=ResumePoint.RUN_PLANNER)

    point = await store.get_resume_point(context_id)

    current_step = None
    last_execution = None
    last_review = None

    if session.current_step_id:
        current_step = await store.get_step(session.current_step_id)
        if current_step:
            last_execution = await store.get_latest_execution(context_id, current_step.id)
            if last_execution:
                last_review = await store.get_latest_review(context_id, current_step.id)

    logger.info("Resume state for %s: %s (step=%s)", context_id, point, session.current_step_id)

    return ResumeState(
        point=point,
        session=session,
        current_step=current_step,
        last_execution=last_execution,
        last_review=last_review,
    )
