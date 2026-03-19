from k8s_debug_agent.config import Settings
from k8s_debug_agent.data_types import PlannerDecision


class LLMConfig:
    def __init__(self, config: Settings):
        self._base_config = {
            "model": config.LLM_MODEL,
            "base_url": config.LLM_API_BASE,
            "api_type": "openai",
            "api_key": config.LLM_API_KEY,
        }

        self.k8s_agent_llm_config = self._create_llm_config(config, None)
        self.github_agent_llm_config = self._create_llm_config(config, None)
        self.planner_llm_config = self._create_llm_config(config, PlannerDecision)

    def _create_llm_config(self, config: Settings, response_format):
        return {
            "config_list": [
                {
                    **self._base_config,
                    **({"response_format": response_format} if response_format else {}),
                    **({"default_headers": config.EXTRA_HEADERS} if config.EXTRA_HEADERS else {}),
                }
            ],
            "temperature": config.LLM_TEMPERATURE,
        }
