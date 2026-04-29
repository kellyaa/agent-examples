import logging
import sys

import uvicorn

from simple_skill_demo.a2a_server import create_app
from simple_skill_demo.config import Settings
from simple_skill_demo.observability import setup_observability

logger = logging.getLogger(__name__)


def run():
    settings = Settings()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )

    setup_observability()

    logger.info("Starting Multi-Stage Skill Demo Agent")
    logger.info("LLM Model: %s", settings.llm_model)
    logger.info("LLM API Base: %s", settings.llm_api_base)
    logger.info("Database: %s", settings.database_url.split("@")[-1] if "@" in settings.database_url else "configured")
    logger.info("Skills directory: %s", settings.skills_dir)

    app = create_app(settings)

    logger.info("Starting A2A server on %s:%d", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
