import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel

from app.config import Settings
from app.logging import configure_logging
from app.db import create_database_engine, create_session_factory
from app.jobs import router, APIError

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_database_engine(settings)
    application.state.session_factory = create_session_factory(engine)
    application.state.settings = settings
    logger.info("api_started")
    try:
        yield
    finally:
        engine.dispose()
        logger.info("api_stopped")


app = FastAPI(title="Document Extraction Service", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(APIError)
async def api_error_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "message": exc.message})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    return JSONResponse(status_code=400, content={"code": "INVALID_REQUEST", "message": "Missing or invalid request fields."})


@app.exception_handler(HTTPException)
async def http_error_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"code": "INVALID_REQUEST", "message": str(exc.detail)})


@app.exception_handler(SQLAlchemyError)
@app.exception_handler(OSError)
async def infrastructure_error_handler(request, exc):
    # Avoid exposing SQL parameters, database credentials or document contents.
    logger.error("request_storage_failed type=%s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"code": "STORAGE_UNAVAILABLE", "message": "Storage is temporarily unavailable."})


@app.get("/health", response_model=HealthResponse, operation_id="health")
def health() -> HealthResponse:
    return HealthResponse()
