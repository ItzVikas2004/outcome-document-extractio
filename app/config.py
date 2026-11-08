from typing import Literal
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://outcome@postgres:5432/outcome"
    )
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_max_calls: int = Field(default=64, ge=1, le=1000)
    llm_total_input_budget: int = Field(default=500000, ge=1)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=2)
    worker_lease_seconds: float = Field(default=30, ge=1, le=120)
    worker_heartbeat_seconds: float = Field(default=5, gt=0)
    worker_max_attempts: int = Field(default=3, ge=1, le=10)
    worker_retry_base_seconds: float = Field(default=2, gt=0, le=30)
    upload_dir: Path = Path("uploads")

    @model_validator(mode="after")
    def validate_worker_timing(self):
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds / 2:
            raise ValueError("Heartbeat interval must be less than half the lease duration")
        return self
