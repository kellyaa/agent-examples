from pydantic import BaseModel, Field
from typing import Literal, Optional


class PlannerDecision(BaseModel):
    action: Literal["delegate_k8s", "delegate_github", "remediate_k8s", "final_report"] = Field(
        description="The next action the planner wants to take"
    )
    instruction: Optional[str] = Field(
        None,
        description="The specific instruction to send to the delegated agent, or the final report content",
    )
    reasoning: str = Field(
        description="Brief explanation of why this action was chosen",
    )
