from datetime import datetime, timezone

from pydantic import BaseModel, Field

from simple_skill_demo.schemas.session import ReviewStatus


class ExecutionResult(BaseModel):
    id: str
    context_id: str
    step_id: str
    skill_name: str = ""
    skill_inputs: dict = Field(default_factory=dict)
    raw_output: str = ""
    summary: str = ""
    error: dict | None = None
    runtime_ms: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReviewResult(BaseModel):
    id: str
    context_id: str
    step_id: str
    review_status: ReviewStatus
    goal_achieved: bool = False
    reason: str = ""
    recommended_action: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlannerLog(BaseModel):
    id: str
    context_id: str
    input_context: dict = Field(default_factory=dict)
    generated_plan: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
