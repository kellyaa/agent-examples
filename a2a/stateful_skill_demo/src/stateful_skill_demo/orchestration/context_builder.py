"""Assemble bounded prior-goal context + artifact listing for the planner.

Called by the runner on every planner invocation. Reads from the SessionStore and
returns two string blocks suitable for inclusion in the planner prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

from stateful_skill_demo.persistence.session_store import SessionStore
from stateful_skill_demo.schemas.artifact import Artifact
from stateful_skill_demo.schemas.goal import Goal
from stateful_skill_demo.schemas.session import StepStatus


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass
class PlannerContext:
    prior_context: str
    artifacts_block: str


async def build_planner_context(
    store: SessionStore,
    context_id: str,
    current_goal_id: str,
    n: int | None,
    char_budget: int,
) -> PlannerContext:
    """Build bounded prior-goal + artifact blocks.

    - Prior goals: last N (or all if n is None), excluding the current goal.
    - Per goal: user_goal + final status + succeeded-step summaries + up to 3 failure tuples.
    - Artifacts: all non-superseded artifacts for the contextId.
    - If total char count exceeds char_budget, drop older goals first, then per-goal failures,
      then per-goal succeeded steps.
    """

    all_goals = await store.get_goals_for_context(context_id)
    prior = [g for g in all_goals if g.id != current_goal_id]
    if n is not None and n >= 0:
        prior = prior[-n:] if n > 0 else []

    goal_blocks: list[tuple[Goal, list[str], list[str]]] = []
    for g in prior:
        succeeded_lines: list[str] = []
        failure_lines: list[str] = []

        steps = await store.get_steps_for_goal(g.id)
        succeeded = [s for s in steps if s.status == StepStatus.SUCCEEDED]
        failed = [s for s in steps if s.status == StepStatus.FAILED]

        for s in succeeded:
            succeeded_lines.append(
                f"  - {_truncate(s.description, 200)} → {_truncate(s.expected_result, 120) or '(no expected result)'}"
            )

        for s in failed[-3:]:
            exec_result = await store.get_latest_execution(context_id, s.id)
            review_result = await store.get_latest_review(context_id, s.id)
            err = ""
            fb = ""
            if exec_result and exec_result.error:
                err = _truncate(str(exec_result.error.get("message", exec_result.error)), 200)
            if review_result:
                fb = _truncate(review_result.recommended_action, 300)
            failure_lines.append(
                f"  - {_truncate(s.description, 200)} [FAILED] error={err!r} feedback={fb!r}"
            )

        goal_blocks.append((g, succeeded_lines, failure_lines))

    artifacts: list[Artifact] = await store.get_artifacts_for_context(context_id)

    def render() -> tuple[str, str]:
        pieces: list[str] = []
        if goal_blocks:
            pieces.append("Prior conversation (most recent first):")
            for g, succ, fails in reversed(goal_blocks):
                pieces.append(f"- Goal #{g.goal_index} ({g.status.value}): {_truncate(g.user_goal, 500)}")
                if succ:
                    pieces.append("  Succeeded steps:")
                    pieces.extend(succ)
                if fails:
                    pieces.append("  Failures:")
                    pieces.extend(fails)
        prior_context = "\n".join(pieces)

        if artifacts:
            lines = ["Available artifacts (reference via @{name} in step inputs):"]
            for a in artifacts:
                stale = " [STALE]" if a.is_stale else ""
                lines.append(
                    f"- {a.name} ({a.kind.value}, v{a.version}){stale}: {_truncate(a.summary, 200)}"
                )
            artifacts_block = "\n".join(lines)
        else:
            artifacts_block = ""
        return prior_context, artifacts_block

    prior_context, artifacts_block = render()

    # Token-bound: drop oldest goal, then oldest failures, then oldest successes.
    def total() -> int:
        return len(prior_context) + len(artifacts_block)

    while total() > char_budget and goal_blocks:
        # drop oldest goal entirely
        goal_blocks.pop(0)
        prior_context, artifacts_block = render()

    # still over? strip failures from remaining goals, oldest first
    i = 0
    while total() > char_budget and i < len(goal_blocks):
        goal_blocks[i] = (goal_blocks[i][0], goal_blocks[i][1], [])
        prior_context, artifacts_block = render()
        i += 1

    # still over? strip successes too
    i = 0
    while total() > char_budget and i < len(goal_blocks):
        goal_blocks[i] = (goal_blocks[i][0], [], [])
        prior_context, artifacts_block = render()
        i += 1

    return PlannerContext(prior_context=prior_context, artifacts_block=artifacts_block)
