from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://outcome@postgres:5432/outcome"
    )
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    worker_poll_interval_seconds: float = Field(default=5.0, gt=0)
