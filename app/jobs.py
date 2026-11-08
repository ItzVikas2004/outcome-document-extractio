import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from pydantic import BaseModel, ConfigDict, HttpUrl, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Job, JobState

MAX_FILE_BYTES = 50 * 1024 * 1024
logger = logging.getLogger(__name__)
router = APIRouter()


class APIError(Exception):
    def __init__(self, status_code, code, message):
        self.status_code, self.code, self.message = status_code, code, message


class Error(BaseModel):
    code: str
    message: str


class JobStatus(BaseModel):
    job_id: str
    status: JobState
    created_at: datetime
    updated_at: datetime
    stage: str | None = None
    error: Error | None = None
    result: dict | None = None


class FieldDefinition(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    name: str
    description: str
    primary: bool | None = None


class FieldSchema(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    name: str
    fields: dict[str, FieldDefinition]


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_schema(value):
    try:
        schema = json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_keys)
        FieldSchema.model_validate(schema)
        canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        # PostgreSQL JSONB cannot represent NUL characters or unpaired surrogates.
        if "\\u0000" in canonical:
            raise ValueError("NUL character")
        json.dumps(schema, ensure_ascii=False).encode("utf-8")
        return schema, hashlib.sha256(canonical.encode()).hexdigest()
    except (ValueError, ValidationError, RecursionError, UnicodeError):
        raise APIError(400, "INVALID_SCHEMA", "schema must be a JSON object with a name and a fields map of names and descriptions.") from None


def tenant_id(value):
    if value is None:
        return ""
    if not value.strip():
        raise APIError(400, "INVALID_ORGANIZATION", "X-Organization-Id must not be empty.")
    return value


def serialize_job(job):
    def aware(value):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    response = dict(job_id=job.job_id, status=job.status,
                    created_at=aware(job.created_at), updated_at=aware(job.updated_at))
    if job.status == JobState.RUNNING and job.stage is not None:
        response["stage"] = job.stage
    if job.error is not None:
        response["error"] = job.error
    if job.status in (JobState.SUCCESS, JobState.PARTIAL) and job.result is not None:
        response["result"] = job.result
    return response


@router.post("/jobs", status_code=202, response_model=JobStatus, response_model_exclude_unset=True,
             operation_id="createJob", responses={400: {"model": Error}, 413: {"description": "File too large."}})
def create_job(request: Request, file: Annotated[UploadFile, File()],
               schema: Annotated[str, Form()], webhook_url: Annotated[HttpUrl | None, Form()] = None,
               organization: Annotated[str | None, Header(alias="X-Organization-Id")] = None):
    tenant = tenant_id(organization)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx", ".xlsx", ".csv"}:
        raise APIError(400, "UNSUPPORTED_FILE", "Supported file extensions: .pdf, .docx, .xlsx, .csv.")
    if file.size is not None and file.size > MAX_FILE_BYTES:
        raise APIError(413, "FILE_TOO_LARGE", "File exceeds 50 MB.")
    field_schema, schema_hash = parse_schema(schema)
    if webhook_url is not None:
        try:
            TypeAdapter(HttpUrl).validate_python(webhook_url)
        except ValidationError:
            raise APIError(400, "INVALID_WEBHOOK", "webhook_url must be an HTTP or HTTPS URL.") from None

    job_id = str(uuid4())
    directory = request.app.state.settings.upload_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (job_id + suffix)
    temporary = directory / (job_id + ".tmp")
    digest = hashlib.sha256()
    size = 0
    retained = False
    try:
        with temporary.open("xb") as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise APIError(413, "FILE_TOO_LARGE", "File exceeds 50 MB.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        if os.name == "posix":
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        document_hash = digest.hexdigest()
        with request.app.state.session_factory() as session:
            query = select(Job).where(Job.organization_id == tenant, Job.document_hash == document_hash,
                                      Job.schema_hash == schema_hash)
            existing = session.scalar(query)
            if existing:
                return serialize_job(existing)
            job = Job(job_id=job_id, organization_id=tenant, file_path=str(destination),
                      max_attempts=request.app.state.settings.worker_max_attempts,
                      field_schema=field_schema, document_hash=document_hash,
                      schema_hash=schema_hash, webhook_url=str(webhook_url) if webhook_url is not None else None)
            session.add(job)
            # A dropped connection during COMMIT can leave its outcome unknown.
            # Retain the file in that case rather than risking a dangling job.
            retained = True
            try:
                session.commit()
                session.refresh(job)  # Read database-assigned submission timestamps.
            except IntegrityError:
                retained = False
                session.rollback()
                existing = session.scalar(query)
                if existing:
                    return serialize_job(existing)
                raise
            retained = True
            logger.info("job_accepted job_id=%s", job_id)
            return serialize_job(job)
    finally:
        temporary.unlink(missing_ok=True)
        if not retained:
            destination.unlink(missing_ok=True)


@router.get("/jobs/{job_id}", response_model=JobStatus, response_model_exclude_unset=True,
            operation_id="getJob", responses={404: {"description": "Unknown job."}})
def get_job(job_id: str, request: Request,
            organization: Annotated[str | None, Header(alias="X-Organization-Id")] = None):
    tenant = tenant_id(organization)
    try:
        canonical_id = str(UUID(job_id))
    except ValueError:
        raise APIError(404, "JOB_NOT_FOUND", "Job not found.") from None
    with request.app.state.session_factory() as session:
        job = session.scalar(select(Job).where(Job.job_id == canonical_id, Job.organization_id == tenant))
        if job is None:
            raise APIError(404, "JOB_NOT_FOUND", "Job not found.")
        return serialize_job(job)
