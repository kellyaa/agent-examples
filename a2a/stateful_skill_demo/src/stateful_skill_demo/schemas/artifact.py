from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class ArtifactKind(StrEnum):
    TEXT = "text"
    JSON = "json"
    FILE_PATH = "file_path"
    URL = "url"


class Artifact(BaseModel):
    id: str
    context_id: str
    goal_id: str
    step_id: str | None = None
    name: str
    kind: ArtifactKind
    value: str
    summary: str = ""
    version: int = 1
    is_stale: bool = False
    superseded_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
