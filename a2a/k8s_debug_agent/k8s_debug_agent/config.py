import json
import logging
import os
from typing import Any, Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):

    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default=os.getenv("LOG_LEVEL", "INFO"),
        description="Application log level",
    )

    # A2A Server
    A2A_HOST: str = Field(
        default=os.getenv("A2A_HOST", "0.0.0.0"),
        description="Host address for A2A server",
    )
    SERVICE_PORT: int = Field(
        default=int(os.getenv("SERVICE_PORT", "8000")),
        description="Port for A2A server",
    )
    A2A_PUBLIC_URL: Optional[str] = Field(
        default=os.getenv("A2A_PUBLIC_URL"),
        description="Publicly routable A2A base URL for agent discovery",
    )

    # MCP Servers
    K8S_MCP: str = Field(
        default=os.getenv("K8S_MCP", ""),
        description="Streamable HTTP endpoint for the Kubernetes MCP server",
    )
    GITHUB_MCP: str = Field(
        default=os.getenv("GITHUB_MCP", ""),
        description="Streamable HTTP endpoint for the GitHub MCP server",
    )
    GITHUB_TOKEN: str = Field(
        default=os.getenv("GITHUB_TOKEN", ""),
        description="GitHub personal access token for authenticating with the GitHub MCP server",
    )

    # LLM
    LLM_MODEL: str = Field(
        default=os.getenv("LLM_MODEL", "gpt-4"),
        description="Model identifier",
    )
    LLM_API_BASE: str = Field(
        default=os.getenv("LLM_API_BASE", "https://api.openai.com/v1"),
        description="Base URL for the OpenAI-compatible API",
    )
    LLM_API_KEY: str = Field(
        default=os.getenv("LLM_API_KEY", ""),
        description="API key for the OpenAI-compatible LLM endpoint",
    )
    LLM_TEMPERATURE: float = Field(
        default=float(os.getenv("LLM_TEMPERATURE", "0")),
        description="Sampling temperature",
    )
    EXTRA_HEADERS: dict[str, str] = Field(
        default_factory=dict,
        description="Extra headers for the LLM API (JSON string)",
    )

    @field_validator("EXTRA_HEADERS", mode="before")
    @classmethod
    def _parse_extra_headers(cls, v: Any) -> dict[str, str]:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return {}
            return json.loads(v)
        if v is None:
            return {}
        return v

    # Execution Limits
    MAX_PLAN_STEPS: int = Field(
        default=int(os.getenv("MAX_PLAN_STEPS", "15")),
        description="Maximum planner loop iterations",
        ge=1,
    )
    MAX_TOOL_RESPONSE_CHARS: int = Field(
        default=int(os.getenv("MAX_TOOL_RESPONSE_CHARS", "50000")),
        description="Maximum characters per MCP tool response (0 = unlimited)",
        ge=0,
    )

    # Tracing
    OTEL_CONSOLE_TRACING: bool = Field(
        default=os.getenv("OTEL_CONSOLE_TRACING", "false").lower() in ("true", "1", "yes"),
        description="Print OpenTelemetry traces to console when no OTLP endpoint is configured",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()  # type: ignore[call-arg]
