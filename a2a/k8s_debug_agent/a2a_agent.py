"""A2A server entry point for the Kubernetes Debug Agent."""

import contextlib
import logging
import sys
import traceback

import httpx
import uvicorn
from autogen.mcp.mcp_client import create_toolkit, Toolkit
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Part,
    TaskState,
    TextPart,
)
from a2a.utils import new_agent_text_message, new_task

from starlette.routing import Route

from k8s_debug_agent.config import settings
from k8s_debug_agent.event import Event
from k8s_debug_agent.main import K8sDebugAgent
from k8s_debug_agent.tracing import init_tracing

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format="%(levelname)s: %(message)s")


def get_agent_card(host: str, port: int) -> AgentCard:
    capabilities = AgentCapabilities(streaming=True)
    skill = AgentSkill(
        id="k8s_debug_agent",
        name="Kubernetes Debug Agent",
        description=(
            "Diagnoses root causes of application failures running on Kubernetes. "
            "Inspects cluster state (pods, events, logs) and correlates with recent "
            "code changes from GitHub to produce a structured root-cause analysis report."
        ),
        tags=["kubernetes", "k8s", "debug", "diagnosis", "github", "root-cause-analysis"],
        examples=[
            "My app in namespace production is crash-looping, repo is org/myapp",
            "The frontend deployment in staging is returning 500 errors, check github.com/org/frontend",
            "Pods in namespace dev keep getting OOMKilled for the api-server deployment",
        ],
    )

    agent_url = settings.A2A_PUBLIC_URL
    if not agent_url:
        if host == "0.0.0.0":
            agent_url = f"http://localhost:{port}/"
        else:
            agent_url = f"http://{host}:{port}/"

    return AgentCard(
        name="Kubernetes Debug Agent",
        description="Diagnoses Kubernetes application failures by correlating cluster state with GitHub code changes",
        url=agent_url,
        version="0.1.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=capabilities,
        skills=[skill],
    )


class A2AEvent(Event):
    def __init__(self, task_updater: TaskUpdater):
        self.task_updater = task_updater

    async def emit_event(self, message: str, final: bool = False) -> None:
        logger.info("Emitting event: %s (final=%s)", message, final)

        if final:
            parts = [Part(root=TextPart(text=message))]
            await self.task_updater.add_artifact(parts)
            await self.task_updater.complete()
        else:
            await self.task_updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    message,
                    self.task_updater.context_id,
                    self.task_updater.task_id,
                ),
            )


class K8sDebugExecutor(AgentExecutor):

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        user_input = [context.get_user_input()]
        task = context.current_task
        if not task:
            task = new_task(context.message)  # type: ignore
            await event_queue.enqueue_event(task)
        task_updater = TaskUpdater(event_queue, task.id, task.context_id)
        event_emitter = A2AEvent(task_updater)

        messages = [{"role": "User", "content": message} for message in user_input]

        tracer_provider = init_tracing()

        try:
            async with contextlib.AsyncExitStack() as stack:
                k8s_toolkit = await self._connect_mcp(stack, settings.K8S_MCP, "K8s")

                github_headers = {}
                if settings.GITHUB_TOKEN:
                    github_headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
                github_toolkit = await self._connect_mcp(stack, settings.GITHUB_MCP, "GitHub", headers=github_headers)

                await self._run_agent(messages, event_emitter, k8s_toolkit, github_toolkit, tracer_provider)

        except Exception as e:
            traceback.print_exc()
            logger.error("Error executing task: %s", e, exc_info=True)
            await event_emitter.emit_event(
                f"I encountered an error while diagnosing the failure: {str(e)}",
                True,
            )

    async def _connect_mcp(
        self,
        stack: contextlib.AsyncExitStack,
        url: str,
        label: str,
        headers: dict[str, str] | None = None,
    ) -> Toolkit | None:
        if not url:
            logger.info("No %s MCP URL configured, skipping", label)
            return None
        logger.info("Connecting to %s MCP server at %s", label, url)
        http_client = None
        if headers:
            http_client = await stack.enter_async_context(httpx.AsyncClient(headers=headers))
        transport = await stack.enter_async_context(
            streamable_http_client(url=url, http_client=http_client)
        )
        read_stream, write_stream, _ = transport
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        toolkit = await create_toolkit(session=session, use_mcp_resources=False)
        logger.info("Loaded %d tools from %s MCP: %s", len(toolkit.tools), label, [t.name for t in toolkit.tools])
        return toolkit

    async def _run_agent(
        self,
        messages: list[dict],
        event_emitter: Event,
        k8s_toolkit: Toolkit | None,
        github_toolkit: Toolkit | None,
        tracer_provider=None,
    ):
        agent = K8sDebugAgent(
            config=settings,
            eventer=event_emitter,
            k8s_toolkit=k8s_toolkit,
            github_toolkit=github_toolkit,
            tracer_provider=tracer_provider,
        )
        result = await agent.execute(messages)
        await event_emitter.emit_event(result, True)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Task cancellation is not supported")


def run():
    agent_card = get_agent_card(host=settings.A2A_HOST, port=settings.SERVICE_PORT)

    request_handler = DefaultRequestHandler(
        agent_executor=K8sDebugExecutor(),
        task_store=InMemoryTaskStore(),
    )

    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    app = server.build()

    app.routes.insert(
        0,
        Route(
            "/.well-known/agent-card.json",
            server._handle_get_agent_card,
            methods=["GET"],
            name="agent_card_new",
        ),
    )

    uvicorn.run(app, host=settings.A2A_HOST, port=settings.SERVICE_PORT)
