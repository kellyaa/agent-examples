import json
import logging
from datetime import datetime, timezone
from enum import StrEnum

from simple_skill_demo.persistence.database import Database
from simple_skill_demo.schemas.execution_result import ExecutionResult, PlannerLog, ReviewResult
from simple_skill_demo.schemas.plan_step import PlanStep
from simple_skill_demo.schemas.session import Session, SessionStatus, StepStatus

logger = logging.getLogger(__name__)


class ResumePoint(StrEnum):
    RUN_PLANNER = "RUN_PLANNER"
    RUN_EXECUTOR = "RUN_EXECUTOR"
    RUN_REVIEWER = "RUN_REVIEWER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SessionStore:
    def __init__(self, db: Database):
        self._db = db

    # ── sessions ──

    async def create_session(self, context_id: str, user_goal: str) -> Session:
        now = datetime.now(timezone.utc)
        await self._db.pool.execute(
            """INSERT INTO sessions (context_id, user_goal, status, failure_count, created_at, updated_at)
               VALUES ($1, $2, 'ACTIVE', 0, $3, $3)""",
            context_id,
            user_goal,
            now,
        )
        return Session(context_id=context_id, user_goal=user_goal, created_at=now, updated_at=now)

    async def get_session(self, context_id: str) -> Session | None:
        row = await self._db.pool.fetchrow("SELECT * FROM sessions WHERE context_id = $1", context_id)
        if not row:
            return None
        return Session(
            context_id=row["context_id"],
            user_goal=row["user_goal"],
            status=SessionStatus(row["status"]),
            current_step_id=row["current_step_id"],
            failure_count=row["failure_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def update_session(self, context_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(timezone.utc)
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
        values = [context_id, *fields.values()]
        await self._db.pool.execute(f"UPDATE sessions SET {sets} WHERE context_id = $1", *values)

    # ── plan_steps ──

    async def create_step(self, step: PlanStep) -> None:
        await self._db.pool.execute(
            """INSERT INTO plan_steps (id, context_id, step_order, description, inputs_json,
               expected_result, status, retry_count, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            step.id,
            step.context_id,
            step.step_order,
            step.description,
            json.dumps(step.inputs),
            step.expected_result,
            step.status.value,
            step.retry_count,
            step.created_at,
            step.updated_at,
        )

    async def get_steps(self, context_id: str) -> list[PlanStep]:
        rows = await self._db.pool.fetch(
            "SELECT * FROM plan_steps WHERE context_id = $1 ORDER BY step_order", context_id
        )
        return [
            PlanStep(
                id=r["id"],
                context_id=r["context_id"],
                step_order=r["step_order"],
                description=r["description"],
                inputs=json.loads(r["inputs_json"]) if isinstance(r["inputs_json"], str) else r["inputs_json"],
                expected_result=r["expected_result"],
                status=StepStatus(r["status"]),
                retry_count=r["retry_count"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def get_step(self, step_id: str) -> PlanStep | None:
        r = await self._db.pool.fetchrow("SELECT * FROM plan_steps WHERE id = $1", step_id)
        if not r:
            return None
        return PlanStep(
            id=r["id"],
            context_id=r["context_id"],
            step_order=r["step_order"],
            description=r["description"],
            inputs=json.loads(r["inputs_json"]) if isinstance(r["inputs_json"], str) else r["inputs_json"],
            expected_result=r["expected_result"],
            status=StepStatus(r["status"]),
            retry_count=r["retry_count"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    async def update_step(self, step_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(timezone.utc)
        if "status" in fields and isinstance(fields["status"], StepStatus):
            fields["status"] = fields["status"].value
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
        values = [step_id, *fields.values()]
        await self._db.pool.execute(f"UPDATE plan_steps SET {sets} WHERE id = $1", *values)

    # ── execution_logs ──

    async def log_execution(self, result: ExecutionResult) -> None:
        await self._db.pool.execute(
            """INSERT INTO execution_logs (id, context_id, step_id, skill_name, skill_inputs_json,
               raw_output, summary, error_json, runtime_ms, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            result.id,
            result.context_id,
            result.step_id,
            result.skill_name,
            json.dumps(result.skill_inputs),
            result.raw_output,
            result.summary,
            json.dumps(result.error) if result.error else None,
            result.runtime_ms,
            result.created_at,
        )

    async def get_latest_execution(self, context_id: str, step_id: str) -> ExecutionResult | None:
        r = await self._db.pool.fetchrow(
            """SELECT * FROM execution_logs
               WHERE context_id = $1 AND step_id = $2
               ORDER BY created_at DESC LIMIT 1""",
            context_id,
            step_id,
        )
        if not r:
            return None
        return ExecutionResult(
            id=r["id"],
            context_id=r["context_id"],
            step_id=r["step_id"],
            skill_name=r["skill_name"],
            skill_inputs=(
                json.loads(r["skill_inputs_json"]) if isinstance(r["skill_inputs_json"], str) else r["skill_inputs_json"]
            ),
            raw_output=r["raw_output"],
            summary=r["summary"],
            error=json.loads(r["error_json"]) if isinstance(r["error_json"], str) else r["error_json"],
            runtime_ms=r["runtime_ms"],
            created_at=r["created_at"],
        )

    # ── review_logs ──

    async def log_review(self, review: ReviewResult) -> None:
        await self._db.pool.execute(
            """INSERT INTO review_logs (id, context_id, step_id, review_status, goal_achieved,
               reason, recommended_action, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            review.id,
            review.context_id,
            review.step_id,
            review.review_status.value,
            review.goal_achieved,
            review.reason,
            review.recommended_action,
            review.created_at,
        )

    async def get_latest_review(self, context_id: str, step_id: str) -> ReviewResult | None:
        r = await self._db.pool.fetchrow(
            """SELECT * FROM review_logs
               WHERE context_id = $1 AND step_id = $2
               ORDER BY created_at DESC LIMIT 1""",
            context_id,
            step_id,
        )
        if not r:
            return None
        from simple_skill_demo.schemas.session import ReviewStatus

        return ReviewResult(
            id=r["id"],
            context_id=r["context_id"],
            step_id=r["step_id"],
            review_status=ReviewStatus(r["review_status"]),
            goal_achieved=r["goal_achieved"],
            reason=r["reason"],
            recommended_action=r["recommended_action"],
            created_at=r["created_at"],
        )

    # ── planner_logs ──

    async def log_planner(self, log: PlannerLog) -> None:
        await self._db.pool.execute(
            """INSERT INTO planner_logs (id, context_id, input_context, generated_plan_json, created_at)
               VALUES ($1, $2, $3, $4, $5)""",
            log.id,
            log.context_id,
            json.dumps(log.input_context),
            json.dumps(log.generated_plan),
            log.created_at,
        )

    # ── resume ──

    async def get_resume_point(self, context_id: str) -> ResumePoint:
        session = await self.get_session(context_id)
        if not session:
            return ResumePoint.RUN_PLANNER
        if session.status == SessionStatus.COMPLETED:
            return ResumePoint.COMPLETED
        if session.status == SessionStatus.FAILED:
            return ResumePoint.FAILED

        if not session.current_step_id:
            return ResumePoint.RUN_PLANNER

        execution = await self.get_latest_execution(context_id, session.current_step_id)
        if not execution:
            return ResumePoint.RUN_EXECUTOR

        review = await self.get_latest_review(context_id, session.current_step_id)
        if not review:
            return ResumePoint.RUN_REVIEWER

        return ResumePoint.RUN_PLANNER
