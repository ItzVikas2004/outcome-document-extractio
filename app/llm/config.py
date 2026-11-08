import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from app.extraction import ExtractionFailure, FailureCode

ALLOWED_MODELS = frozenset({
    "google/gemini-3.8-flash", "google/gemini-3.1-pro-preview",
    "anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5",
    "openai/gpt-6-luna", "openai/gpt-6.1-sol", "z-ai/glm-5.3-flash",
    "qwen/qwen3-vl-235b-a22b-instruct", "deepseek/deepseek-v3.2",
})


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str = "google/gemini-3.8-flash"
    max_input_tokens: int = 16000
    max_output_tokens: int = 8192
    total_input_budget: int = 500000
    max_calls: int = 64
    transport_retries: int = 2
    repair_retries: int = 1
    timeout_seconds: int = 45
    input_price_per_million: Decimal | None = None
    output_price_per_million: Decimal | None = None

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        valid = (self.model in ALLOWED_MODELS and self.api_key.strip()
                 and parsed.scheme in {"http", "https"} and parsed.netloc
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        numbers = (self.max_input_tokens, self.max_output_tokens, self.total_input_budget, self.max_calls, self.timeout_seconds)
        rates = (self.input_price_per_million, self.output_price_per_million)
        if (not valid or any(type(n) is not int or n <= 0 for n in numbers)
                or not 0 <= self.transport_retries <= 5 or not 0 <= self.repair_retries <= 3
                or any(rate is not None and (not rate.is_finite() or rate < 0) for rate in rates)):
            raise ExtractionFailure(FailureCode.LLM_CONFIGURATION, retryable=False)

    @classmethod
    def from_env(cls):
        # Read at execution time; intentionally no dotenv, endpoint fallback or key cache.
        base, key = os.environ.get("LLM_BASE_URL", ""), os.environ.get("LLM_API_KEY", "")
        if not base or not key:
            raise ExtractionFailure(FailureCode.EXTRACTION_NOT_CONFIGURED, retryable=False)
        try:
            values = {name: int(os.environ[env]) for name, env in {
                "max_input_tokens": "LLM_MAX_INPUT_TOKENS", "max_output_tokens": "LLM_MAX_OUTPUT_TOKENS",
                "total_input_budget": "LLM_TOTAL_INPUT_BUDGET", "max_calls": "LLM_MAX_CALLS",
                "transport_retries": "LLM_TRANSPORT_RETRIES", "repair_retries": "LLM_REPAIR_RETRIES",
                "timeout_seconds": "LLM_TIMEOUT_SECONDS"}.items() if os.environ.get(env)}
            for name, env in (("input_price_per_million", "LLM_INPUT_PRICE_PER_MILLION"),
                              ("output_price_per_million", "LLM_OUTPUT_PRICE_PER_MILLION")):
                if os.environ.get(env):
                    values[name] = Decimal(os.environ[env])
            return cls(base, key, model=os.environ.get("LLM_MODEL", "google/gemini-3.8-flash"), **values)
        except (ValueError, InvalidOperation):
            raise ExtractionFailure(FailureCode.LLM_CONFIGURATION, retryable=False) from None
