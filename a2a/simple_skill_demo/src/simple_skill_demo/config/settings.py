from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_KEYS = {"dummy", "changeme", "your-api-key-here", ""}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_api_base: str = "http://localhost:11434/v1"
    llm_api_key: str = "dummy"
    llm_model: str = "llama3.1"
    llm_temperature: float = 0.0

    database_url: str = "postgresql://postgres:postgres@localhost:5432/skill_demo"

    skills_dir: str = ".skills"

    max_step_retries: int = 3
    max_total_failures: int = 10

    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    @property
    def has_valid_api_key(self) -> bool:
        host = urlparse(self.llm_api_base).hostname or ""
        if host in {"localhost", "127.0.0.1", "0.0.0.0", "dockerhost", "host.docker.internal"}:
            return True
        if host.endswith(".svc.cluster.local"):
            return True
        return self.llm_api_key.strip() not in _PLACEHOLDER_KEYS
