"""Tests for stateful_skill_demo — settings, schemas, resume, artifact kind detection,
and the run_turn emission contract."""

import ast
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock heavy dependencies before importing the runner (which transitively pulls autogen
# via the agents and asyncpg via the session store).
for mod in [
    "autogen",
    "autogen.beta",
    "autogen.beta.config",
    "autogen.beta.tools",
    "autogen.beta.events",
    "asyncpg",
]:
    sys.modules.setdefault(mod, MagicMock())

from stateful_skill_demo.config.settings import Settings
from stateful_skill_demo.schemas.artifact import Artifact, ArtifactKind
from stateful_skill_demo.schemas.execution_result import ExecutionResult, PlannerLog, ReviewResult
from stateful_skill_demo.schemas.goal import Goal
from stateful_skill_demo.schemas.plan_step import PlanStep
from stateful_skill_demo.schemas.session import GoalStatus, ReviewStatus, StepStatus
from stateful_skill_demo.schemas.user_turn import UserTurn


class TestSettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("LLM_API_BASE", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("SKILLS_DIR", raising=False)
        settings = Settings()
        assert settings.llm_api_base == "http://localhost:11434/v1"
        assert settings.llm_api_key == "dummy"
        assert settings.llm_model == "llama3.1"
        assert settings.skills_dir == ".skills"
        assert settings.max_step_retries == 3
        assert settings.max_total_failures == 10
        assert settings.planner_prior_goals_n is None
        assert settings.planner_context_char_budget == 8000
        assert settings.host == "0.0.0.0"
        assert settings.port == 8000

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("LLM_API_BASE", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test-key")
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db:5432/test")
        monkeypatch.setenv("SKILLS_DIR", "/custom/skills")
        monkeypatch.setenv("MAX_STEP_RETRIES", "5")
        monkeypatch.setenv("PLANNER_PRIOR_GOALS_N", "3")
        settings = Settings()
        assert settings.llm_model == "gpt-4o-mini"
        assert settings.llm_api_base == "https://api.openai.com/v1"
        assert settings.llm_api_key == "sk-test-key"
        assert settings.database_url == "postgresql://user:pass@db:5432/test"
        assert settings.skills_dir == "/custom/skills"
        assert settings.max_step_retries == 5
        assert settings.planner_prior_goals_n == 3

    def test_has_valid_api_key_local(self, monkeypatch):
        monkeypatch.setenv("LLM_API_BASE", "http://localhost:11434/v1")
        monkeypatch.setenv("LLM_API_KEY", "dummy")
        settings = Settings()
        assert settings.has_valid_api_key is True

    def test_has_valid_api_key_remote_placeholder(self, monkeypatch):
        monkeypatch.setenv("LLM_API_BASE", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "dummy")
        settings = Settings()
        assert settings.has_valid_api_key is False

    def test_has_valid_api_key_remote_real(self, monkeypatch):
        monkeypatch.setenv("LLM_API_BASE", "https://api.openai.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-real-key-12345")
        settings = Settings()
        assert settings.has_valid_api_key is True


class TestEnums:
    def test_goal_status_values(self):
        assert GoalStatus.ACTIVE == "ACTIVE"
        assert GoalStatus.COMPLETED == "COMPLETED"
        assert GoalStatus.FAILED == "FAILED"
        assert GoalStatus.SUPERSEDED == "SUPERSEDED"

    def test_step_status_values(self):
        assert StepStatus.PENDING == "PENDING"
        assert StepStatus.IN_PROGRESS == "IN_PROGRESS"
        assert StepStatus.SUCCEEDED == "SUCCEEDED"
        assert StepStatus.FAILED == "FAILED"
        assert StepStatus.REPLACED == "REPLACED"
        assert StepStatus.SKIPPED == "SKIPPED"

    def test_review_status_values(self):
        assert ReviewStatus.SUCCESS == "SUCCESS"
        assert ReviewStatus.PARTIAL_SUCCESS == "PARTIAL_SUCCESS"
        assert ReviewStatus.FAILURE == "FAILURE"

    def test_artifact_kind_values(self):
        assert ArtifactKind.TEXT == "text"
        assert ArtifactKind.JSON == "json"
        assert ArtifactKind.FILE_PATH == "file_path"
        assert ArtifactKind.URL == "url"


class TestSchemas:
    def test_plan_step_defaults(self):
        step = PlanStep(id="s1", context_id="ctx1", goal_id="g1", step_order=1, description="do something")
        assert step.status == StepStatus.PENDING
        assert step.retry_count == 0
        assert step.inputs == {}
        assert step.expected_result == ""
        assert step.expected_artifact_name is None

    def test_execution_result_with_error(self):
        result = ExecutionResult(
            id="e1",
            context_id="ctx1",
            goal_id="g1",
            step_id="s1",
            error={"type": "ValueError", "message": "bad input"},
        )
        assert result.error is not None
        assert result.error["type"] == "ValueError"
        assert result.runtime_ms == 0
        assert result.artifact_id is None

    def test_review_result(self):
        review = ReviewResult(
            id="r1",
            context_id="ctx1",
            goal_id="g1",
            step_id="s1",
            review_status=ReviewStatus.SUCCESS,
            goal_achieved=True,
            reason="Output matched expected result",
        )
        assert review.goal_achieved is True
        assert review.review_status == ReviewStatus.SUCCESS

    def test_planner_log(self):
        log = PlannerLog(
            id="p1",
            context_id="ctx1",
            goal_id="g1",
            input_context={"user_goal": "test"},
            generated_plan={"goal_complete": False, "step": {"description": "do it"}},
        )
        assert log.input_context["user_goal"] == "test"
        assert log.goal_id == "g1"

    def test_goal_defaults(self):
        goal = Goal(id="g1", context_id="ctx1", goal_index=1, user_goal="do a thing")
        assert goal.status == GoalStatus.ACTIVE
        assert goal.failure_count == 0
        assert goal.current_step_id is None

    def test_user_turn_nullable_goal(self):
        turn = UserTurn(id="t1", context_id="ctx1", turn_index=1, text="hello")
        assert turn.goal_id is None

    def test_artifact_defaults(self):
        art = Artifact(
            id="a1",
            context_id="ctx1",
            goal_id="g1",
            name="file_list",
            kind=ArtifactKind.TEXT,
            value="some output",
        )
        assert art.version == 1
        assert art.is_stale is False
        assert art.superseded_by is None


class TestResumeLogic:
    def test_resume_point_enum(self):
        from stateful_skill_demo.persistence.session_store import ResumePoint

        assert ResumePoint.RUN_PLANNER == "RUN_PLANNER"
        assert ResumePoint.RUN_EXECUTOR == "RUN_EXECUTOR"
        assert ResumePoint.RUN_REVIEWER == "RUN_REVIEWER"
        assert ResumePoint.COMPLETED == "COMPLETED"
        assert ResumePoint.FAILED == "FAILED"
        assert ResumePoint.AWAIT_NEW_GOAL == "AWAIT_NEW_GOAL"

    def test_resume_state_creation(self):
        from stateful_skill_demo.orchestration.resume import ResumeState
        from stateful_skill_demo.persistence.session_store import ResumePoint

        state = ResumeState(point=ResumePoint.RUN_PLANNER)
        assert state.point == ResumePoint.RUN_PLANNER
        assert state.goal is None
        assert state.current_step is None


class TestArtifactKindDetection:
    def test_detect_url(self):
        from stateful_skill_demo.orchestration.runner import _detect_artifact_kind

        assert _detect_artifact_kind("https://example.com/path", None) == ArtifactKind.URL
        assert _detect_artifact_kind("http://x.y", None) == ArtifactKind.URL

    def test_detect_json_object(self):
        from stateful_skill_demo.orchestration.runner import _detect_artifact_kind

        assert _detect_artifact_kind('{"a": 1}', None) == ArtifactKind.JSON

    def test_detect_json_array(self):
        from stateful_skill_demo.orchestration.runner import _detect_artifact_kind

        assert _detect_artifact_kind("[1, 2, 3]", None) == ArtifactKind.JSON

    def test_detect_text_fallback(self):
        from stateful_skill_demo.orchestration.runner import _detect_artifact_kind

        assert _detect_artifact_kind("just some prose", None) == ArtifactKind.TEXT

    def test_hint_overrides(self):
        from stateful_skill_demo.orchestration.runner import _detect_artifact_kind

        # Hint wins even when heuristic would pick something else.
        assert _detect_artifact_kind("https://x.y", "text") == ArtifactKind.TEXT


class TestRunTurnEmissionContract:
    """Assert every return statement inside run_turn is preceded by an emit(..., final=True).

    This protects against the original bug class where terminal short-circuits dead-ended
    with a plain return and left the A2A event queue open.
    """

    def test_every_return_has_preceding_final_emit(self):
        from stateful_skill_demo.orchestration import runner as runner_module

        source = Path(inspect.getsourcefile(runner_module)).read_text()
        tree = ast.parse(source)

        run_turn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_turn":
                run_turn = node
                break

        assert run_turn is not None, "run_turn function not found"

        returns = [n for n in ast.walk(run_turn) if isinstance(n, ast.Return)]
        assert returns, "expected at least one return in run_turn"

        # Walk through the parent bodies; for each Return, check the statements
        # preceding it in the same block include an emit(..., final=True) call,
        # or the return is inside an `if not emitted_final:` guarded fallback.
        for ret in returns:
            assert _return_is_guarded(run_turn, ret), (
                f"Return at line {ret.lineno} in run_turn is not preceded by emit(final=True)."
            )


def _return_is_guarded(func_node: ast.AST, target_return: ast.Return) -> bool:
    """Walk func_node; find the enclosing block of target_return; verify the block or an
    ancestor block contains an emit(..., final=True) call before this return, OR the
    enclosing if-branch is the emitted_final fallback (which calls emit itself)."""

    def find_enclosing(node, ret):
        # recursively walk; return list of enclosing blocks (each a list of stmts) up to ret
        stack = [(node, [])]
        while stack:
            current, path = stack.pop()
            for field in ("body", "orelse", "finalbody"):
                body = getattr(current, field, None)
                if isinstance(body, list):
                    new_path = path + [body]
                    for stmt in body:
                        if stmt is ret:
                            return new_path
                        stack.append((stmt, new_path))
        return None

    path = find_enclosing(func_node, target_return)
    if path is None:
        return False

    # Check every enclosing block from innermost outward for a preceding final emit.
    # An emit nested inside an If/Try in a preceding statement counts — either that
    # branch fired it, or a branch upstream already did (tracked by emitted_final).
    for block in reversed(path):
        idx = block.index(target_return) if target_return in block else len(block)
        for stmt in block[:idx]:
            if _contains_final_emit(stmt):
                return True
    return False


def _contains_final_emit(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if _is_final_emit(n):
            return True
    return False


def _is_final_emit(stmt: ast.AST) -> bool:
    """True if stmt is `await emit(..., final=True)` (or same via expression)."""
    if isinstance(stmt, ast.Expr):
        stmt = stmt.value
    if isinstance(stmt, ast.Await):
        call = stmt.value
    elif isinstance(stmt, ast.Call):
        call = stmt
    else:
        return False
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    name = None
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    if name != "emit":
        return False
    for kw in call.keywords:
        if kw.arg == "final" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    # positional final=True (emit is (msg, final))
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant) and call.args[1].value is True:
        return True
    return False
