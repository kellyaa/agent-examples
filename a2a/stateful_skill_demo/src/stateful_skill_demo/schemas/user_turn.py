from datetime import datetime, timezone

from pydantic import BaseModel, Field


class UserTurn(BaseModel):
    id: str
    context_id: str
    goal_id: str | None = None
    turn_index: int
    text: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
