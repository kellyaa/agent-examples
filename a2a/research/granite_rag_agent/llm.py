from pydantic import BaseModel
from granite_rag_agent.config import Settings



class LLMConfig:
    def __init__(self, config: Settings):

        self.ollama_llm_config = {
            "config_list": [
                {
                    "model": config.TASK_MODEL_ID,  # Converted to uppercase
                    "base_url": config.OPENAI_API_URL,  # Converted to uppercase
                    "api_type": "openai",
                    "api_key": config.OPENAI_API_KEY,  # Converted to uppercase
                    **({"default_headers": config.EXTRA_HEADERS} if config.EXTRA_HEADERS else {}),
                }
            ],
            "temperature": config.MODEL_TEMPERATURE
        }

        class Plan(BaseModel):
            steps: list[str]

        self.planner_llm_config = {
            "config_list": [
                {
                    "model": config.TASK_MODEL_ID,  # Converted to uppercase
                    "base_url": config.OPENAI_API_URL,  # Converted to uppercase
                    "api_type": "openai",
                    "api_key": config.OPENAI_API_KEY,  # Converted to uppercase
                    "response_format": Plan,
                    **({"default_headers": config.EXTRA_HEADERS} if config.EXTRA_HEADERS else {}),
                }
            ],
            "temperature": config.MODEL_TEMPERATURE
        }

        self.vision_llm_config = {
            "config_list": [
                {
                    "model": config.VISION_MODEL_ID,  # Converted to uppercase
                    "base_url": config.VISION_API_URL,  # Converted to uppercase
                    "api_type": "openai",
                    "api_key": config.OPENAI_API_KEY,  # Converted to uppercase
                    **({"default_headers": config.EXTRA_HEADERS} if config.EXTRA_HEADERS else {}),
                }
            ],
            "temperature": config.MODEL_TEMPERATURE
        }

