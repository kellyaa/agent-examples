"""
Module for A2A Agent.
"""

import logging
from typing import Callable

import uvicorn
from autogen.mcp.mcp_client import create_toolkit, Toolkit
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, TaskState
from a2a.utils import new_agent_text_message, new_task

from granite_rag_agent.config import settings, Settings
from granite_rag_agent.event import Event
from granite_rag_agent.keycloak import get_token
from granite_rag_agent.main import RagAgent
from granite_rag_agent.tools.tavily_search import search

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def get_agent_card(host: str, port: int):
    """Returns the Agent Card for the AG2 Agent."""
    capabilities = AgentCapabilities(streaming=True)
    skill = AgentSkill(
        id="web_researcher",
        name="Web research agent",
        description="Perform research using web searches",
        tags=["research", "internet", "search", "report"],
        examples=[
            "Find me the latest news on AI agents",
            "Write a report about the latest academic papers on bubble gum",
        ],
    )
    return AgentCard(
        name="Web Research Agent",
        description="Perform research using web searches",
        url=f"http://{host}:{port}/",
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=capabilities,
        skills=[skill],
    )


class A2AEvent(Event):
    """
    A class to handle events for A2A Agent.

    Attributes:
        task_updater (TaskUpdater): The task updater instance.
    """

    def __init__(self, task_updater: TaskUpdater):
        """
        Initializes the A2AEvent instance.

        Args:
            task_updater (TaskUpdater): The task updater instance.
        """
        self.task_updater = task_updater

    async def emit_event(self, message: str, final: bool = False) -> None:
        """
        Emits an event with the given message.

        Args:
            message (str): The event message.
            final (bool): Whether the event is final. Defaults to False.
        """
        logger.info("Emitting event %s", message)

        if final:
            await self.task_updater.complete(
                new_agent_text_message(
                    message, self.task_updater.context_id, self.task_updater.task_id
                )
            )
        else:
            await self.task_updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    message,
                    self.task_updater.context_id,
                    self.task_updater.task_id,
                ),
            )


class ResearchExecutor(AgentExecutor):
    """
    A class to handle research execution for A2A Agent.
    """
    async def _run_agent(self,
        messages: dict,
        settings: Settings,
        event_emitter: Event,
        assistant_tool_map: dict[str, Callable],
        toolkit: Toolkit):

        rag_agent = RagAgent(
            config=settings,
            eventer=event_emitter,
            assistant_tools=assistant_tool_map,
            mcp_toolkit=toolkit,
        )
        result = await rag_agent.run_workflow(messages)
        await event_emitter.emit_event(result, True)

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        """
        Executes the research task.

        Args:
            context (RequestContext): The request context.
            event_queue (EventQueue): The event queue instance.

        Returns:
            None
        """
        user_input = [context.get_user_input()]
        task = context.current_task
        if not task:
            task = new_task(context.message)  # type: ignore
            await event_queue.enqueue_event(task)
        task_updater = TaskUpdater(event_queue, task.id, task.context_id)
        event_emitter = A2AEvent(task_updater)
        messages = []
        for message in user_input:
            messages.append(
                {
                    "role": "User",
                    "content": message,
                }
            )

        search_tool = search
        assistant_tool_map = {"web_search": search_tool}
        try:
            toolkit = None
            if settings.MCP_ENDPOINT:
                # Need to auth with keycloak for our PoC in order to use remote MCP
                if settings.KEYCLOAK_URL:
                        try:
                            token = get_token()
                            logger.info(f'received token: {token}')
                        except Exception as e:
                            logger.error("Unable to retrieve keycloak token: %s", e)
                            
                if settings.MCP_TRANSPORT == "sse":
                    async with sse_client(
                        url=settings.MCP_ENDPOINT
                    ) as streams, ClientSession(*streams) as session:
                        await session.initialize()
                        toolkit = await create_toolkit(
                            session=session, use_mcp_resources=False
                        )
                        await self._run_agent(messages, settings,
                            event_emitter,
                            assistant_tool_map,
                            toolkit,)
                else:
                    async with streamablehttp_client(
                        url=settings.MCP_ENDPOINT
                    )  as (
                        read_stream,
                        write_stream,
                        _,
                    ), ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        toolkit = await create_toolkit(
                            session=session, use_mcp_resources=False
                        )
                        await self._run_agent(messages, settings,
                            event_emitter,
                            assistant_tool_map,
                            toolkit,)
            else:
                await self._run_agent(messages, settings,
                    event_emitter,
                    assistant_tool_map,
                    toolkit,)

        except Exception as e:
            logger.error(repr(e))
            raise e

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """
        Not implemented
        """
        raise Exception("cancel not supported")


def run():
    """
    Runs the A2A Agent application.
    """
    agent_card = get_agent_card(host="0.0.0.0", port=8000)

    request_handler = DefaultRequestHandler(
        agent_executor=ResearchExecutor(),
        task_store=InMemoryTaskStore(),
    )

    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    uvicorn.run(server.build(), host="0.0.0.0", port=8000)
