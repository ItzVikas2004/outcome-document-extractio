import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app.config import Settings
from app.logging import configure_logging

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@asynccontextmanager
async def lifespan(application: FastAPI):
    configure_logging(Settings().log_level)
    logger.info("api_started")
    yield
    logger.info("api_stopped")


app = FastAPI(title="Document Extraction Service", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse, operation_id="health")
def health() -> HealthResponse:
    return HealthResponse()
