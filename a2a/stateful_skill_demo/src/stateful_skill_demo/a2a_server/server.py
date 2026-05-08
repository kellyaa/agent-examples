import asyncio
import logging
import os
from typing import Any

from a2a.helpers.proto_helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware

from stateful_skill_demo.agents.executor import ExecutorAgent
from stateful_skill_demo.agents.planner import PlannerAgent
from stateful_skill_demo.agents.responder import ResponderAgent
from stateful_skill_demo.agents.reviewer import ReviewerAgent
from stateful_skill_demo.config.settings import Settings
from stateful_skill_demo.observability import create_tracing_middleware, get_root_span, set_span_output
from stateful_skill_demo.orchestration.runner import OrchestrationRunner
from stateful_skill_demo.persistence.database import Database
from stateful_skill_demo.persistence.session_store import SessionStore

logger = logging.getLogger(__name__)


def get_agent_card(settings: Settings) -> AgentCard:
    capabilities = AgentCapabilities(streaming=True)
    skill = AgentSkill(
        id="skill_demo_agent",
        name="Stateful Skill Demo",
        description=(
            "A general-purpose agent that plans, executes, and reviews tasks "
            "using AG2 native skills. Supports multi-goal conversations with "
            "named artifacts and session resume."
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
        name="Stateful Skill Demo Agent",
        description=(
            "A stateful, multi-stage skill-driven agent using AG2 beta tools. "
            "Plans execution steps, runs them using skills, reviews results, "
            "and persists all state to PostgreSQL. Supports multi-goal "
            "conversations with named artifact reuse across turns."
        ),
        supported_interfaces=[
            AgentInterface(
                url=agent_url,
                protocol_binding="jsonrpc",
                protocol_version="1.0",
            )
        ],
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
        self._context_locks: dict[str, asyncio.Lock] = {}

    async def _ensure_db(self) -> SessionStore:
        if self._store is None:
            self._db = Database(
                self._settings.database_url,
                auto_create=self._settings.database_auto_create,
            )
            await self._db.init()
            self._store = SessionStore(self._db)
        return self._store

    def _lock_for(self, context_id: str) -> asyncio.Lock:
        lock = self._context_locks.get(context_id)
        if lock is None:
            lock = asyncio.Lock()
            self._context_locks[context_id] = lock
        return lock

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        task_updater = TaskUpdater(event_queue, task.id, task.context_id)
        emitted_final = False

        async def event_callback(message: str, final: bool = False) -> None:
            nonlocal emitted_final
            logger.info("Event: %s (final=%s)", message, final)
            if final:
                await task_updater.add_artifact([new_text_part(message)])
                await task_updater.complete()
                emitted_final = True
            else:
                await task_updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    task_updater.new_agent_message([new_text_part(message)]),
                )

        async def error_callback(message: str) -> None:
            nonlocal emitted_final
            logger.error("Error: %s", message)
            await task_updater.add_artifact([new_text_part(message)])
            await task_updater.failed()
            emitted_final = True

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
            responder = ResponderAgent(self._settings)
            runner = OrchestrationRunner(self._settings, store, planner, executor, reviewer, responder)

            async with self._lock_for(context_id):
                await store.ensure_session(context_id)
                turn = await store.create_turn(context_id, user_input)
                result = await runner.run_turn(context_id, turn.id, user_input, event_callback)

            root_span = get_root_span()
            if root_span and root_span.is_recording():
                set_span_output(root_span, result)

        except Exception as exc:
            logger.error("Execution error: %s", exc, exc_info=True)
            await error_callback(f"Error: {exc}")
            return

        # Fallback: runner exited without a final event. Emit a generic one so the
        # A2A event queue closes cleanly instead of leaving the client hanging.
        if not emitted_final:
            logger.warning("run_turn returned without a final event; emitting fallback.")
            await event_callback("Turn completed.", final=True)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Task cancellation is not fully supported")


def create_app(settings: Settings) -> Any:
    agent_card = get_agent_card(settings)

    request_handler = DefaultRequestHandler(
        agent_executor=SkillDemoExecutor(settings),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    routes = [
        *create_agent_card_routes(agent_card),
        *create_jsonrpc_routes(request_handler, rpc_url="/", enable_v0_3_compat=True),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(BaseHTTPMiddleware, dispatch=create_tracing_middleware())
    logger.info("A2A server application created")
    return app
