"""
Observability setup for Stateful Skill Demo Agent.

Auto-detects tracing backend from environment variables:
- OTEL_EXPORTER_OTLP_ENDPOINT -> OpenTelemetry with GenAI semantic conventions
- Neither -> no tracing, warning logged

Follows the GenAI semantic conventions for agent instrumentation:
- GenAI attributes (gen_ai.operation.name, gen_ai.agent.name, etc.)
- MLflow attributes (mlflow.spanInputs, mlflow.spanOutputs, mlflow.spanType)
- OpenInference attributes (openinference.span.kind, input.value, output.value)
"""

import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from opentelemetry import context, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagate import extract, set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

AGENT_NAME = "skill-demo-agent"
AGENT_VERSION = "0.1.0"
AGENT_FRAMEWORK = "ag2"

_root_span_var: ContextVar = ContextVar("root_span", default=None)
_use_otel = False

try:
    from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes

    OPENINFERENCE_AVAILABLE = True
except ImportError:
    OPENINFERENCE_AVAILABLE = False


def get_root_span():
    if not _use_otel:
        return None
    return _root_span_var.get()


def _get_otlp_exporter(endpoint: str):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    if not endpoint.endswith("/v1/traces"):
        endpoint = endpoint.rstrip("/") + "/v1/traces"
    return OTLPSpanExporter(endpoint=endpoint)


def setup_observability() -> None:
    global _use_otel

    use_otel = bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    _use_otel = use_otel

    if not use_otel:
        logger.warning("No tracing backend configured. Set OTEL_EXPORTER_OTLP_ENDPOINT for OpenTelemetry tracing.")
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "skill-demo-service")
    namespace = os.getenv("K8S_NAMESPACE_NAME", "team1")
    otlp_endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://otel-collector.kagenti-system.svc.cluster.local:8335",
    )

    logger.info("Setting up OpenTelemetry observability")
    logger.info("  Service: %s", service_name)
    logger.info("  Namespace: %s", namespace)
    logger.info("  OTLP Endpoint: %s", otlp_endpoint)

    resource = Resource(
        attributes={
            SERVICE_NAME: service_name,
            SERVICE_VERSION: AGENT_VERSION,
            "service.namespace": namespace,
            "k8s.namespace.name": namespace,
            "mlflow.traceName": AGENT_NAME,
            "mlflow.source": service_name,
            "gen_ai.agent.name": AGENT_NAME,
            "gen_ai.agent.version": AGENT_VERSION,
            "gen_ai.system": AGENT_FRAMEWORK,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(_get_otlp_exporter(otlp_endpoint)))
    trace.set_tracer_provider(tracer_provider)

    set_global_textmap(
        CompositePropagator(
            [
                TraceContextTextMapPropagator(),
                W3CBaggagePropagator(),
            ]
        )
    )

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
        logger.info("httpx instrumented for automatic trace context propagation")
    except ImportError:
        pass

    try:
        from opentelemetry.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument()
        logger.info("OpenAI instrumented with GenAI semantic conventions")
    except ImportError:
        pass


_tracer: Optional[trace.Tracer] = None
TRACER_NAME = "openinference.instrumentation.agent"


def get_tracer() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer(TRACER_NAME)
    return _tracer


def set_span_output(span, output: str):
    if not _use_otel:
        return
    if output:
        truncated = str(output)[:1000]
        span.set_attribute("gen_ai.completion", truncated)
        span.set_attribute("output.value", truncated)
        span.set_attribute("mlflow.spanOutputs", truncated)


def set_token_usage(span, input_tokens: int = 0, output_tokens: int = 0):
    if input_tokens:
        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        span.set_attribute("mlflow.span.chat_usage.input_tokens", input_tokens)
    if output_tokens:
        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        span.set_attribute("mlflow.span.chat_usage.output_tokens", output_tokens)


@contextmanager
def create_agent_span(
    name: str = "invoke_agent",
    context_id: Optional[str] = None,
    task_id: Optional[str] = None,
    input_text: Optional[str] = None,
):
    tracer = get_tracer()

    attributes = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.provider.name": AGENT_FRAMEWORK,
        "gen_ai.agent.name": AGENT_NAME,
    }

    if context_id:
        attributes["gen_ai.conversation.id"] = context_id
    if input_text:
        attributes["gen_ai.prompt"] = input_text[:1000]
        attributes["input.value"] = input_text[:1000]
        attributes["mlflow.spanInputs"] = input_text[:1000]
    if task_id:
        attributes["a2a.task_id"] = task_id

    attributes["mlflow.spanType"] = "AGENT"
    attributes["mlflow.traceName"] = AGENT_NAME

    if OPENINFERENCE_AVAILABLE:
        attributes[SpanAttributes.OPENINFERENCE_SPAN_KIND] = OpenInferenceSpanKindValues.AGENT.value

    with tracer.start_as_current_span(name, attributes=attributes) as span:
        try:
            yield span
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            raise


def create_tracing_middleware():
    if not _use_otel:

        async def passthrough_middleware(request, call_next):
            return await call_next(request)

        return passthrough_middleware

    from starlette.requests import Request
    from starlette.responses import Response, StreamingResponse

    async def tracing_middleware(request: Request, call_next):
        if request.url.path in ["/health", "/ready", "/.well-known/agent-card.json", "/.well-known/agent.json"]:
            return await call_next(request)

        tracer = get_tracer()

        user_input = None
        context_id = None
        message_id = None

        try:
            body = await request.body()
            if body:
                data = json.loads(body)
                params = data.get("params", {})
                message = params.get("message", {})
                parts = message.get("parts", [])
                if parts and isinstance(parts, list):
                    user_input = parts[0].get("text", "")
                context_id = params.get("contextId") or message.get("contextId")
                message_id = message.get("messageId")
        except Exception as e:
            logger.debug("Could not parse request body: %s", e)

        incoming_ctx = extract(dict(request.headers))
        detach_token = context.attach(incoming_ctx)

        try:
            span_name = f"invoke_agent {AGENT_NAME}"

            with tracer.start_as_current_span(
                span_name,
                kind=SpanKind.INTERNAL,
            ) as span:
                span_token = _root_span_var.set(span)

                # GenAI Semantic Conventions (Required)
                span.set_attribute("gen_ai.operation.name", "invoke_agent")
                span.set_attribute("gen_ai.provider.name", AGENT_FRAMEWORK)
                span.set_attribute("gen_ai.agent.name", AGENT_NAME)
                span.set_attribute("gen_ai.agent.version", AGENT_VERSION)

                if user_input:
                    span.set_attribute("gen_ai.prompt", user_input[:1000])
                    span.set_attribute("input.value", user_input[:1000])
                    span.set_attribute("mlflow.spanInputs", user_input[:1000])

                session_id = context_id or message_id
                if session_id:
                    span.set_attribute("gen_ai.conversation.id", session_id)
                    span.set_attribute("mlflow.trace.session", session_id)
                    span.set_attribute("session.id", session_id)

                # MLflow trace metadata
                span.set_attribute("mlflow.spanType", "AGENT")
                span.set_attribute("mlflow.traceName", AGENT_NAME)
                span.set_attribute("mlflow.runName", f"{AGENT_NAME}-invoke")
                span.set_attribute("mlflow.source", "skill-demo-service")
                span.set_attribute("mlflow.version", AGENT_VERSION)

                auth_header = request.headers.get("authorization", "")
                if auth_header:
                    span.set_attribute("mlflow.user", "authenticated")
                    span.set_attribute("enduser.id", "authenticated")
                else:
                    span.set_attribute("mlflow.user", "anonymous")
                    span.set_attribute("enduser.id", "anonymous")

                # OpenInference span kind (Phoenix)
                if OPENINFERENCE_AVAILABLE:
                    span.set_attribute(
                        SpanAttributes.OPENINFERENCE_SPAN_KIND,
                        OpenInferenceSpanKindValues.AGENT.value,
                    )

                try:
                    response = await call_next(request)

                    if isinstance(response, Response) and not isinstance(response, StreamingResponse):
                        response_body = b""
                        async for chunk in response.body_iterator:
                            response_body += chunk

                        try:
                            if response_body:
                                resp_data = json.loads(response_body)
                                result = resp_data.get("result", {})
                                artifacts = result.get("artifacts", [])
                                if artifacts:
                                    resp_parts = artifacts[0].get("parts", [])
                                    if resp_parts:
                                        output_text = resp_parts[0].get("text", "")
                                        if output_text:
                                            set_span_output(span, output_text)
                        except Exception as e:
                            logger.debug("Could not parse response body: %s", e)

                        span.set_status(Status(StatusCode.OK))
                        return Response(
                            content=response_body,
                            status_code=response.status_code,
                            headers=dict(response.headers),
                            media_type=response.media_type,
                        )

                    span.set_status(Status(StatusCode.OK))
                    return response

                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise
                finally:
                    _root_span_var.reset(span_token)
        finally:
            context.detach(detach_token)

    return tracing_middleware
