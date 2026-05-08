import json
import logging
import uuid
from datetime import datetime, timezone
from enum import StrEnum

import asyncpg

from stateful_skill_demo.persistence.database import Database
from stateful_skill_demo.schemas.artifact import Artifact, ArtifactKind
from stateful_skill_demo.schemas.execution_result import ExecutionResult, PlannerLog, ReviewResult
from stateful_skill_demo.schemas.goal import Goal
from stateful_skill_demo.schemas.plan_step import PlanStep
from stateful_skill_demo.schemas.session import GoalStatus, ReviewStatus, Session, StepStatus
from stateful_skill_demo.schemas.user_turn import UserTurn

logger = logging.getLogger(__name__)


class ResumePoint(StrEnum):
    RUN_PLANNER = "RUN_PLANNER"
    RUN_EXECUTOR = "RUN_EXECUTOR"
    RUN_REVIEWER = "RUN_REVIEWER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    AWAIT_NEW_GOAL = "AWAIT_NEW_GOAL"


Executor = asyncpg.Pool | asyncpg.Connection


class SessionStore:
    def __init__(self, db: Database):
        self._db = db

    def _exec(self, conn: asyncpg.Connection | None) -> Executor:
        return conn if conn is not None else self._db.pool

    # ── sessions ──

    async def ensure_session(self, context_id: str, *, conn: asyncpg.Connection | None = None) -> Session:
        executor = self._exec(conn)
        now = datetime.now(timezone.utc)
        await executor.execute(
            """INSERT INTO sessions (context_id, created_at, updated_at)
               VALUES ($1, $2, $2)
               ON CONFLICT (context_id) DO NOTHING""",
            context_id,
            now,
        )
        row = await executor.fetchrow(
            "SELECT * FROM sessions WHERE context_id = $1", context_id
        )
        return Session(
            context_id=row["context_id"],
            current_goal_id=row["current_goal_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_session(self, context_id: str) -> Session | None:
        row = await self._db.pool.fetchrow("SELECT * FROM sessions WHERE context_id = $1", context_id)
        if not row:
            return None
        return Session(
            context_id=row["context_id"],
            current_goal_id=row["current_goal_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _update_session(
        self, context_id: str, *, conn: asyncpg.Connection | None = None, **fields
    ) -> None:
        fields["updated_at"] = datetime.now(timezone.utc)
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
        values = [context_id, *fields.values()]
        executor = self._exec(conn)
        await executor.execute(
            f"UPDATE sessions SET {sets} WHERE context_id = $1", *values
        )

    # ── user_turns ──

    async def create_turn(
        self, context_id: str, text: str, *, conn: asyncpg.Connection | None = None
    ) -> UserTurn:
        """Insert a new user turn with auto-incrementing turn_index. goal_id is NULL until bound."""
        turn_id = f"turn_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        executor = self._exec(conn)
        row = await executor.fetchrow(
            """INSERT INTO user_turns (id, context_id, turn_index, text, received_at)
               VALUES (
                   $1, $2,
                   COALESCE((SELECT MAX(turn_index) FROM user_turns WHERE context_id = $2), 0) + 1,
                   $3, $4
               )
               RETURNING *""",
            turn_id,
            context_id,
            text,
            now,
        )
        return UserTurn(
            id=row["id"],
            context_id=row["context_id"],
            goal_id=row["goal_id"],
            turn_index=row["turn_index"],
            text=row["text"],
            received_at=row["received_at"],
        )

    async def bind_turn_to_goal(
        self, turn_id: str, goal_id: str, *, conn: asyncpg.Connection | None = None
    ) -> None:
        await self._exec(conn).execute(
            "UPDATE user_turns SET goal_id = $2 WHERE id = $1", turn_id, goal_id
        )

    async def get_turns_for_context(self, context_id: str) -> list[UserTurn]:
        rows = await self._db.pool.fetch(
            "SELECT * FROM user_turns WHERE context_id = $1 ORDER BY turn_index",
            context_id,
        )
        return [
            UserTurn(
                id=r["id"],
                context_id=r["context_id"],
                goal_id=r["goal_id"],
                turn_index=r["turn_index"],
                text=r["text"],
                received_at=r["received_at"],
            )
            for r in rows
        ]

    # ── goals ──

    async def create_goal(
        self,
        context_id: str,
        user_goal: str,
        originating_turn_id: str | None = None,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> Goal:
        """Transactionally: compute goal_index, insert goal, update sessions.current_goal_id,
        bind the originating turn to the new goal."""
        goal_id = f"goal_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)

        async def _do(c: asyncpg.Connection) -> Goal:
            row = await c.fetchrow(
                """INSERT INTO goals (id, context_id, goal_index, user_goal, originating_turn_id,
                                      status, failure_count, created_at, updated_at)
                   VALUES (
                       $1, $2,
                       COALESCE((SELECT MAX(goal_index) FROM goals WHERE context_id = $2), 0) + 1,
                       $3, $4, $5, 0, $6, $6
                   )
                   RETURNING *""",
                goal_id,
                context_id,
                user_goal,
                originating_turn_id,
                GoalStatus.ACTIVE.value,
                now,
            )
            await c.execute(
                "UPDATE sessions SET current_goal_id = $2, updated_at = $3 WHERE context_id = $1",
                context_id,
                goal_id,
                now,
            )
            if originating_turn_id:
                await c.execute(
                    "UPDATE user_turns SET goal_id = $2 WHERE id = $1",
                    originating_turn_id,
                    goal_id,
                )
            return Goal(
                id=row["id"],
                context_id=row["context_id"],
                goal_index=row["goal_index"],
                user_goal=row["user_goal"],
                originating_turn_id=row["originating_turn_id"],
                status=GoalStatus(row["status"]),
                current_step_id=row["current_step_id"],
                failure_count=row["failure_count"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

        if conn is not None:
            return await _do(conn)
        async with self._db.pool.acquire() as c:
            async with c.transaction():
                return await _do(c)

    async def get_goal(self, goal_id: str) -> Goal | None:
        r = await self._db.pool.fetchrow("SELECT * FROM goals WHERE id = $1", goal_id)
        if not r:
            return None
        return self._row_to_goal(r)

    async def get_goals_for_context(self, context_id: str) -> list[Goal]:
        rows = await self._db.pool.fetch(
            "SELECT * FROM goals WHERE context_id = $1 ORDER BY goal_index", context_id
        )
        return [self._row_to_goal(r) for r in rows]

    async def get_current_goal(self, context_id: str) -> Goal | None:
        r = await self._db.pool.fetchrow(
            """SELECT g.* FROM goals g
               JOIN sessions s ON s.current_goal_id = g.id
               WHERE s.context_id = $1""",
            context_id,
        )
        if not r:
            return None
        return self._row_to_goal(r)

    async def update_goal(
        self, goal_id: str, *, conn: asyncpg.Connection | None = None, **fields
    ) -> None:
        fields["updated_at"] = datetime.now(timezone.utc)
        if "status" in fields and isinstance(fields["status"], GoalStatus):
            fields["status"] = fields["status"].value
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
        values = [goal_id, *fields.values()]
        await self._exec(conn).execute(
            f"UPDATE goals SET {sets} WHERE id = $1", *values
        )

    async def supersede_active_goals(
        self,
        context_id: str,
        except_goal_id: str | None = None,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> list[str]:
        """Mark any ACTIVE goals (other than except_goal_id) as SUPERSEDED. Returns the ids marked."""
        now = datetime.now(timezone.utc)
        if except_goal_id is None:
            rows = await self._exec(conn).fetch(
                """UPDATE goals SET status = 'SUPERSEDED', updated_at = $2
                   WHERE context_id = $1 AND status = 'ACTIVE'
                   RETURNING id""",
                context_id,
                now,
            )
        else:
            rows = await self._exec(conn).fetch(
                """UPDATE goals SET status = 'SUPERSEDED', updated_at = $2
                   WHERE context_id = $1 AND status = 'ACTIVE' AND id <> $3
                   RETURNING id""",
                context_id,
                now,
                except_goal_id,
            )
        return [r["id"] for r in rows]

    @staticmethod
    def _row_to_goal(r) -> Goal:
        return Goal(
            id=r["id"],
            context_id=r["context_id"],
            goal_index=r["goal_index"],
            user_goal=r["user_goal"],
            originating_turn_id=r["originating_turn_id"],
            status=GoalStatus(r["status"]),
            current_step_id=r["current_step_id"],
            failure_count=r["failure_count"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    # ── plan_steps ──

    async def create_step(self, step: PlanStep, *, conn: asyncpg.Connection | None = None) -> None:
        await self._exec(conn).execute(
            """INSERT INTO plan_steps (id, context_id, goal_id, step_order, description, inputs_json,
               expected_result, expected_artifact_name, status, retry_count, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
            step.id,
            step.context_id,
            step.goal_id,
            step.step_order,
            step.description,
            json.dumps(step.inputs),
            step.expected_result,
            step.expected_artifact_name,
            step.status.value,
            step.retry_count,
            step.created_at,
            step.updated_at,
        )

    async def get_steps_for_goal(self, goal_id: str) -> list[PlanStep]:
        rows = await self._db.pool.fetch(
            "SELECT * FROM plan_steps WHERE goal_id = $1 ORDER BY step_order", goal_id
        )
        return [self._row_to_step(r) for r in rows]

    async def get_step(self, step_id: str) -> PlanStep | None:
        r = await self._db.pool.fetchrow("SELECT * FROM plan_steps WHERE id = $1", step_id)
        if not r:
            return None
        return self._row_to_step(r)

    @staticmethod
    def _row_to_step(r) -> PlanStep:
        return PlanStep(
            id=r["id"],
            context_id=r["context_id"],
            goal_id=r["goal_id"],
            step_order=r["step_order"],
            description=r["description"],
            inputs=json.loads(r["inputs_json"]) if isinstance(r["inputs_json"], str) else r["inputs_json"],
            expected_result=r["expected_result"],
            expected_artifact_name=r["expected_artifact_name"],
            status=StepStatus(r["status"]),
            retry_count=r["retry_count"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    async def update_step(
        self, step_id: str, *, conn: asyncpg.Connection | None = None, **fields
    ) -> None:
        fields["updated_at"] = datetime.now(timezone.utc)
        if "status" in fields and isinstance(fields["status"], StepStatus):
            fields["status"] = fields["status"].value
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
        values = [step_id, *fields.values()]
        await self._exec(conn).execute(
            f"UPDATE plan_steps SET {sets} WHERE id = $1", *values
        )

    # ── execution_logs ──

    async def log_execution(
        self, result: ExecutionResult, *, conn: asyncpg.Connection | None = None
    ) -> None:
        await self._exec(conn).execute(
            """INSERT INTO execution_logs (id, context_id, goal_id, step_id, artifact_id,
               skill_name, skill_inputs_json, raw_output, summary, error_json, runtime_ms, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
            result.id,
            result.context_id,
            result.goal_id,
            result.step_id,
            result.artifact_id,
            result.skill_name,
            json.dumps(result.skill_inputs),
            result.raw_output,
            result.summary,
            json.dumps(result.error) if result.error else None,
            result.runtime_ms,
            result.created_at,
        )

    async def set_execution_artifact(
        self,
        execution_id: str,
        artifact_id: str,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        await self._exec(conn).execute(
            "UPDATE execution_logs SET artifact_id = $2 WHERE id = $1",
            execution_id,
            artifact_id,
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
            goal_id=r["goal_id"],
            step_id=r["step_id"],
            skill_name=r["skill_name"],
            skill_inputs=(
                json.loads(r["skill_inputs_json"])
                if isinstance(r["skill_inputs_json"], str)
                else r["skill_inputs_json"]
            ),
            raw_output=r["raw_output"],
            summary=r["summary"],
            error=json.loads(r["error_json"]) if isinstance(r["error_json"], str) else r["error_json"],
            runtime_ms=r["runtime_ms"],
            artifact_id=r["artifact_id"],
            created_at=r["created_at"],
        )

    # ── review_logs ──

    async def log_review(
        self, review: ReviewResult, *, conn: asyncpg.Connection | None = None
    ) -> None:
        await self._exec(conn).execute(
            """INSERT INTO review_logs (id, context_id, goal_id, step_id, review_status,
               goal_achieved, reason, recommended_action, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            review.id,
            review.context_id,
            review.goal_id,
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
        return ReviewResult(
            id=r["id"],
            context_id=r["context_id"],
            goal_id=r["goal_id"],
            step_id=r["step_id"],
            review_status=ReviewStatus(r["review_status"]),
            goal_achieved=r["goal_achieved"],
            reason=r["reason"],
            recommended_action=r["recommended_action"],
            created_at=r["created_at"],
        )

    # ── planner_logs ──

    async def log_planner(self, log: PlannerLog, *, conn: asyncpg.Connection | None = None) -> None:
        await self._exec(conn).execute(
            """INSERT INTO planner_logs (id, context_id, goal_id, input_context,
               generated_plan_json, created_at)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            log.id,
            log.context_id,
            log.goal_id,
            json.dumps(log.input_context),
            json.dumps(log.generated_plan),
            log.created_at,
        )

    # ── artifacts ──

    async def create_artifact(
        self, artifact: Artifact, *, conn: asyncpg.Connection | None = None
    ) -> Artifact:
        """Create an artifact. If one with (context_id, name) exists and is not superseded,
        mark it superseded and bump the new artifact's version."""

        async def _do(c: asyncpg.Connection) -> Artifact:
            existing = await c.fetchrow(
                """SELECT id, version FROM artifacts
                   WHERE context_id = $1 AND name = $2 AND superseded_by IS NULL""",
                artifact.context_id,
                artifact.name,
            )
            version = artifact.version
            if existing:
                version = existing["version"] + 1
            row = await c.fetchrow(
                """INSERT INTO artifacts (id, context_id, goal_id, step_id, name, kind, value,
                                          summary, version, is_stale, superseded_by, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                   RETURNING *""",
                artifact.id,
                artifact.context_id,
                artifact.goal_id,
                artifact.step_id,
                artifact.name,
                artifact.kind.value,
                artifact.value,
                artifact.summary,
                version,
                artifact.is_stale,
                None,
                artifact.created_at,
            )
            if existing:
                await c.execute(
                    "UPDATE artifacts SET superseded_by = $2 WHERE id = $1",
                    existing["id"],
                    artifact.id,
                )
            return self._row_to_artifact(row)

        if conn is not None:
            return await _do(conn)
        async with self._db.pool.acquire() as c:
            async with c.transaction():
                return await _do(c)

    async def get_artifact_by_name(self, context_id: str, name: str) -> Artifact | None:
        r = await self._db.pool.fetchrow(
            """SELECT * FROM artifacts
               WHERE context_id = $1 AND name = $2 AND superseded_by IS NULL""",
            context_id,
            name,
        )
        if not r:
            return None
        return self._row_to_artifact(r)

    async def get_artifacts_for_context(
        self, context_id: str, include_superseded: bool = False
    ) -> list[Artifact]:
        if include_superseded:
            rows = await self._db.pool.fetch(
                "SELECT * FROM artifacts WHERE context_id = $1 ORDER BY created_at",
                context_id,
            )
        else:
            rows = await self._db.pool.fetch(
                """SELECT * FROM artifacts
                   WHERE context_id = $1 AND superseded_by IS NULL
                   ORDER BY created_at""",
                context_id,
            )
        return [self._row_to_artifact(r) for r in rows]

    async def mark_artifact_stale(
        self, artifact_id: str, *, conn: asyncpg.Connection | None = None
    ) -> None:
        await self._exec(conn).execute(
            "UPDATE artifacts SET is_stale = true WHERE id = $1", artifact_id
        )

    @staticmethod
    def _row_to_artifact(r) -> Artifact:
        return Artifact(
            id=r["id"],
            context_id=r["context_id"],
            goal_id=r["goal_id"],
            step_id=r["step_id"],
            name=r["name"],
            kind=ArtifactKind(r["kind"]),
            value=r["value"],
            summary=r["summary"],
            version=r["version"],
            is_stale=r["is_stale"],
            superseded_by=r["superseded_by"],
            created_at=r["created_at"],
        )

    # ── resume ──

    async def get_resume_point_for_current_goal(
        self, context_id: str
    ) -> tuple[ResumePoint, Goal | None]:
        """Resolve resume-point scoped to the current goal on the session.

        - No session / no current goal → AWAIT_NEW_GOAL (server should create one).
        - Current goal COMPLETED/FAILED/SUPERSEDED → AWAIT_NEW_GOAL.
        - Otherwise walk step state as before.
        """
        goal = await self.get_current_goal(context_id)
        if not goal:
            return ResumePoint.AWAIT_NEW_GOAL, None
        if goal.status != GoalStatus.ACTIVE:
            return ResumePoint.AWAIT_NEW_GOAL, goal

        if not goal.current_step_id:
            return ResumePoint.RUN_PLANNER, goal

        execution = await self.get_latest_execution(context_id, goal.current_step_id)
        if not execution:
            return ResumePoint.RUN_EXECUTOR, goal

        review = await self.get_latest_review(context_id, goal.current_step_id)
        if not review:
            return ResumePoint.RUN_REVIEWER, goal

        return ResumePoint.RUN_PLANNER, goal
