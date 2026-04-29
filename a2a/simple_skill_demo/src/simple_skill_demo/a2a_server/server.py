import logging
import os
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Part, TaskState, TextPart
from a2a.utils import new_agent_text_message, new_task
from starlette.middleware.base import BaseHTTPMiddleware

from simple_skill_demo.agents.executor import ExecutorAgent
from simple_skill_demo.agents.planner import PlannerAgent
from simple_skill_demo.agents.reviewer import ReviewerAgent
from simple_skill_demo.config.settings import Settings
from simple_skill_demo.observability import create_tracing_middleware, get_root_span, set_span_output
from simple_skill_demo.orchestration.runner import OrchestrationRunner
from simple_skill_demo.persistence.database import Database
from simple_skill_demo.persistence.session_store import SessionStore

logger = logging.getLogger(__name__)


def get_agent_card(settings: Settings) -> AgentCard:
    capabilities = AgentCapabilities(streaming=True)
    skill = AgentSkill(
        id="skill_demo_agent",
        name="Multi-Stage Skill Demo",
        description=(
            "A general-purpose agent that plans, executes, and reviews tasks "
            "using AG2 native skills. Supports session persistence and resume."
        ),
        tags=["skills", "planning", "multi-stage", "a2a"],
        examples=[
            "List available skills and describe them",
            "Search for a React best-practices skill and summarize the top rules",
            "Create a file with a summary of the current directory",
        ],
    )

    host = settings.host
    port = settings.port
    agent_url = os.getenv("AGENT_ENDPOINT", f"http://{host}:{port}").rstrip("/") + "/"

    return AgentCard(
        name="Multi-Stage Skill Demo Agent",
        description=(
            "A multi-stage skill-driven agent using AG2 beta tools. "
            "Plans execution steps, runs them using skills, reviews results, "
            "and persists all state to PostgreSQL for session resume."
        ),
        url=agent_url,
        version="0.1.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=capabilities,
        skills=[skill],
    )


class SkillDemoExecutor(AgentExecutor):
    def __init__(self, settings: Settings):
        self._settings = settings
        self._db: Database | None = None
        self._store: SessionStore | None = None

    async def _ensure_db(self) -> SessionStore:
        if self._store is None:
            self._db = Database(self._settings.database_url)
            await self._db.init()
            self._store = SessionStore(self._db)
        return self._store

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        task_updater = TaskUpdater(event_queue, task.id, task.context_id)

        async def event_callback(message: str, final: bool = False) -> None:
            logger.info("Event: %s (final=%s)", message, final)
            if final:
                parts = [Part(root=TextPart(text=message))]
                await task_updater.add_artifact(parts)
                await task_updater.complete()
            else:
                await task_updater.update_status(
                    TaskState.working,
                    new_agent_text_message(message, task_updater.context_id, task_updater.task_id),
                )

        async def error_callback(message: str) -> None:
            logger.error("Error: %s", message)
            parts = [Part(root=TextPart(text=message))]
            await task_updater.add_artifact(parts)
            await task_updater.failed()

        if not self._settings.has_valid_api_key:
            await error_callback("Error: No LLM API key configured. Set the LLM_API_KEY environment variable.")
            return

        user_input = context.get_user_input()
        context_id = task.context_id
        logger.info("Processing request for context %s: %s", context_id, user_input[:100])

        try:
            store = await self._ensure_db()
            planner = PlannerAgent(self._settings)
            executor = ExecutorAgent(self._settings)
            reviewer = ReviewerAgent(self._settings)
            runner = OrchestrationRunner(self._settings, store, planner, executor, reviewer)

            result = await runner.run_session(context_id, user_input, event_callback)

            root_span = get_root_span()
            if root_span and root_span.is_recording():
                set_span_output(root_span, result)

            await event_callback(result, final=True)

        except Exception as exc:
            logger.error("Execution error: %s", exc, exc_info=True)
            await error_callback(f"Error: {exc}")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if self._store and context.current_task:
            context_id = context.current_task.context_id
            await self._store.update_session(context_id, status="FAILED")
        raise NotImplementedError("Task cancellation is not fully supported")


def create_app(settings: Settings) -> Any:
    agent_card = get_agent_card(settings)

    request_handler = DefaultRequestHandler(
        agent_executor=SkillDemoExecutor(settings),
        task_store=InMemoryTaskStore(),
    )

    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    app = server.build()
    app.add_middleware(BaseHTTPMiddleware, dispatch=create_tracing_middleware())
    logger.info("A2A server application created")
    return app
