import logging
import uvicorn

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    TaskState,
    AgentCapabilities, AgentCard, AgentSkill
)
from a2a.utils import new_agent_text_message, new_task

from autogen.mcp.mcp_client import create_toolkit
from mcp.client.sse import sse_client
from mcp import ClientSession

from granite_rag_agent.config import settings
from granite_rag_agent.main import RagAgent
from granite_rag_agent.event import Event
from granite_rag_agent.tools.tavily_search import search


def get_agent_card(host: str, port: int):
    """Returns the Agent Card for the AG2 Agent."""
    capabilities = AgentCapabilities(streaming=True)
    skill = AgentSkill(
        id='web_researcher',
        name='Web research agent',
        description='Perform research using web searches',
        tags=['research', 'internet', 'search', 'report'],
        examples=[
            'Find me the latest news on AI agents',
            'Write a report about the latest academic papers on bubble gum',
        ],
    )
    return AgentCard(
        name='Web Research Agent',
        description='Perform research using web searches',
        url=f'http://{host}:{port}/',
        version='1.0.0',
        default_input_modes=['text'],
        default_output_modes=['text'],
        capabilities=capabilities,
        skills=[skill],
    )

class A2AEvent(Event):
    def __init__(self, task_updater: TaskUpdater):
        self.task_updater = task_updater

    async def emit_event(self, message, event_type="text", final=False):
        print(f"Emitting event {message}")
        
        if final:
            await self.task_updater.complete(new_agent_text_message(message, self.task_updater.context_id, self.task_updater.task_id))
        else:
            await self.task_updater.update_status(TaskState.working, new_agent_text_message(message, self.task_updater.context_id, self.task_updater.task_id, ))

logging.basicConfig(level=logging.DEBUG)


class ResearchExecutor(AgentExecutor):
    def __init__(self):
        pass

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        input = [context.get_user_input()]
        task = context.current_task
        if not task:
            task = new_task(context.message)  # type: ignore
            await event_queue.enqueue_event(task)
        task_updater = TaskUpdater(event_queue, task.id, task.context_id)
        event_emitter = A2AEvent(task_updater)
        messages = []
        for message in input:
                messages.append({
                    'role': 'User',
                    'content': message,
                })

        search_tool = search
        assistant_tool_map = {"web_search": search_tool}
        try:
            toolkit = None
            if settings.MCP_ENDPOINT:
                async with sse_client(url=settings.MCP_ENDPOINT) as streams, ClientSession(*streams) as session:
                    await session.initialize()
                    toolkit = await create_toolkit(session=session, use_mcp_resources=False)
                    rag_agent = RagAgent(config=settings, eventer=event_emitter, assistant_tools=assistant_tool_map, mcp_toolkit=toolkit)
                    result = await rag_agent.run_workflow(messages)
                    await event_emitter.emit_event(result, True)
            else:
                rag_agent = RagAgent(config=settings, eventer=event_emitter, assistant_tools=assistant_tool_map, mcp_toolkit=toolkit)
                result = await rag_agent.run_workflow(messages)
                await event_emitter.emit_event(result, True)


        except Exception as e:
            print(repr(e))
            raise e
        
        
        
    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        raise Exception('cancel not supported')



def run():
    agent_card = get_agent_card(host='0.0.0.0', port=8000)

    request_handler = DefaultRequestHandler(
        agent_executor=ResearchExecutor(),
        task_store=InMemoryTaskStore(),
    )

    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    uvicorn.run(server.build(), host='0.0.0.0', port=8000)