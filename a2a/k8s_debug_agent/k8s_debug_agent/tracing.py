import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, ConsoleSpanExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from autogen.opentelemetry import instrument_llm_wrapper, instrument_agent

logger = logging.getLogger(__name__)

_SERVICE_NAME = "k8s_debug_agent"

_AGENT_IDS: dict[str, str] = {
    "Planner": "planner-001",
    "K8s_Agent": "k8s-agent-001",
    "GitHub_Agent": "github-agent-001",
    "User": "user-proxy-001",
}


class AgentIdSpanProcessor(SpanProcessor):
    """Injects gen_ai.agent.id on spans that carry gen_ai.agent.name."""

    def __init__(self, agent_ids: dict[str, str]) -> None:
        self._agent_ids = agent_ids

    def on_start(self, span: ReadableSpan, parent_context=None) -> None:
        agent_name = span.attributes.get("gen_ai.agent.name") if span.attributes else None
        if agent_name and agent_name in self._agent_ids:
            span.set_attribute("gen_ai.agent.id", self._agent_ids[agent_name])

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


_tracer_provider: TracerProvider | None = None
_tracing_initialized = False


def init_tracing() -> TracerProvider:
    """Initialize the OpenTelemetry TracerProvider and instrument LLM calls.

    Safe to call multiple times -- only the first call has any effect.
    """
    global _tracer_provider, _tracing_initialized
    if _tracing_initialized:
        return _tracer_provider  # type: ignore[return-value]

    resource = Resource.create(attributes={"service.name": _SERVICE_NAME})
    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(AgentIdSpanProcessor(_AGENT_IDS))

    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        _tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        logger.info(
            "OpenTelemetry tracing enabled (OTLP endpoint: %s)",
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"],
        )
    elif os.environ.get("OTEL_CONSOLE_TRACING", "").lower() in ("true", "1", "yes"):
        _tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        logger.info("OpenTelemetry tracing enabled (console exporter)")

    trace.set_tracer_provider(_tracer_provider)
    instrument_llm_wrapper(tracer_provider=_tracer_provider)

    _tracing_initialized = True
    return _tracer_provider
