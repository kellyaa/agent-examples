from datetime import datetime, timezone

from pydantic import BaseModel, Field

from stateful_skill_demo.schemas.session import StepStatus


class PlanStep(BaseModel):
    id: str
    context_id: str
    goal_id: str
    step_order: int
    description: str
    inputs: dict = Field(default_factory=dict)
    expected_result: str = ""
    expected_artifact_name: str | None = None
    status: StepStatus = StepStatus.PENDING
    retry_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
