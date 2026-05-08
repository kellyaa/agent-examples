from datetime import datetime, timezone

from pydantic import BaseModel, Field

from stateful_skill_demo.schemas.session import GoalStatus


class Goal(BaseModel):
    id: str
    context_id: str
    goal_index: int
    user_goal: str
    originating_turn_id: str | None = None
    status: GoalStatus = GoalStatus.ACTIVE
    current_step_id: str | None = None
    failure_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
