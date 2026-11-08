import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.jobs import MAX_FILE_BYTES
from app.models import Job

SCHEMA = {"name": "unseen-schema", "fields": {"arbitrary_key": {"name": "Any field", "description": "Read it"}}}


def submit(client, extension="pdf", content=b"synthetic document", organization=None, schema=None, **data):
    headers = {} if organization is None else {"X-Organization-Id": organization}
    return client.post("/jobs", files={"file": (f"document.{extension}", content)},
                       data={"schema": json.dumps(SCHEMA) if schema is None else schema, **data}, headers=headers)


@pytest.mark.parametrize("extension", ["pdf", "docx", "xlsx", "csv", "PDF"])
def test_upload_persists_and_roundtrips(client, extension):
    response = submit(client, extension, organization="tenant-a", webhook_url="https://example.test/hook")
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"job_id", "status", "created_at", "updated_at"}
    assert body["status"] == "PENDING"
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None
    assert body["created_at"] == body["updated_at"] or datetime.fromisoformat(body["updated_at"]) >= datetime.fromisoformat(body["created_at"])
    assert client.get(f"/jobs/{body['job_id']}", headers={"X-Organization-Id": "tenant-a"}).json() == body
    with client.session_factory() as session:
        job = session.get(Job, body["job_id"])
        assert job.organization_id == "tenant-a"
        assert job.field_schema == SCHEMA
        assert job.document_hash == hashlib.sha256(b"synthetic document").hexdigest()
        assert len(job.schema_hash) == 64
        assert Path(job.file_path).read_bytes() == b"synthetic document"
        assert job.retry_count == 0 and job.result is None and job.error is None
        assert job.webhook_url == "https://example.test/hook"


@pytest.mark.parametrize("schema", ["{", "null", "[]", "{}", '{"name":"x","fields":[]}',
    '{"name":"x","fields":{"a":{"name":"a"}}}', '{"name":"x","fields":{},"bad":NaN}',
    '{"name":"x","name":"y","fields":{}}'])
def test_malformed_schema(client, schema):
    response = submit(client, schema=schema)
    assert response.status_code == 400
    assert set(response.json()) == {"code", "message"}
    with client.session_factory() as session:
        assert session.scalar(select(Job)) is None
    assert not client.upload_dir.exists()


@pytest.mark.parametrize("extension", ["txt", "exe", "pdf.exe", ""])
def test_unsupported_file(client, extension):
    assert submit(client, extension).status_code == 400


def test_missing_fields_and_malformed_multipart(client):
    assert client.post("/jobs", data={"schema": json.dumps(SCHEMA)}).status_code == 400
    assert client.post("/jobs", files={"file": ("a.pdf", b"a")}).status_code == 400
    response = client.post("/jobs", content=b"broken", headers={"Content-Type": "multipart/form-data"})
    assert response.status_code == 400
    assert set(response.json()) == {"code", "message"}


@pytest.mark.parametrize("extra,expected", [(0, 202), (1, 413)])
def test_real_50mb_boundary(client, tmp_path, extra, expected):
    path = tmp_path / "large.csv"
    with path.open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES + extra)
    with path.open("rb") as handle:
        response = submit(client, "csv", handle)
    assert response.status_code == expected, response.text
    if expected == 413:
        with client.session_factory() as session:
            assert session.scalar(select(Job)) is None
        assert not client.upload_dir.exists()


@pytest.mark.parametrize("identifier", ["not-a-uuid", str(uuid4())])
def test_unknown_job(client, identifier):
    assert client.get(f"/jobs/{identifier}").status_code == 404


def test_tenant_isolation_including_anonymous(client):
    ids = {}
    for tenant in [None, "a", "b", "default"]:
        response = submit(client, organization=tenant)
        assert response.status_code == 202
        ids[tenant] = response.json()["job_id"]
    assert len(set(ids.values())) == 4
    for owner, identifier in ids.items():
        for reader in ids:
            headers = {} if reader is None else {"X-Organization-Id": reader}
            response = client.get(f"/jobs/{identifier}", headers=headers)
            assert response.status_code == (200 if reader == owner else 404)
    assert submit(client, organization=" ").status_code == 400


def test_submission_returns_without_processing(client):
    start = perf_counter()
    response = submit(client)
    elapsed = perf_counter() - start
    assert response.status_code == 202
    assert elapsed < 2.0, f"Submission took {elapsed:.3f}s"
    assert response.json()["status"] == "PENDING"
    assert "result" not in response.json()


def test_idempotency_canonical_schema_and_concurrent_requests(client):
    first = submit(client).json()
    duplicate = submit(client, schema=json.dumps(SCHEMA, sort_keys=True, indent=4)).json()
    assert duplicate == first
    with ThreadPoolExecutor(max_workers=5) as executor:
        responses = list(executor.map(lambda _: submit(client, content=b"concurrent"), range(5)))
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()["job_id"] for response in responses}) == 1
    assert len(list(client.upload_dir.iterdir())) == 2


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "SUCCESS", "PARTIAL", "FAILED"])
def test_status_response_shape(client, state):
    identifier = submit(client).json()["job_id"]
    result = {"entries": [{"arbitrary_key": {"value": None, "citation": "", "confidence": 0}}],
              "metadata": {"model_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0, "latency_ms": 0}}
    with client.session_factory() as session:
        job = session.get(Job, identifier)
        if state != "PENDING":
            job.status = "RUNNING"
            session.flush()
        job.status, job.result, job.stage = state, result, "PARSING"
        job.error = {"code": "TEST_FAILURE", "message": "Synthetic test"} if state == "FAILED" else None
        session.commit()
    body = client.get(f"/jobs/{identifier}").json()
    assert body["status"] == state
    assert ("result" in body) == (state in {"SUCCESS", "PARTIAL"})
    assert ("stage" in body) == (state == "RUNNING")
    assert ("error" in body) == (state == "FAILED")
    if "result" in body:
        assert body["result"] == result


def test_invalid_webhook(client):
    assert submit(client, webhook_url="not a url").status_code == 400


def test_openapi_multipart_contract(client):
    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/jobs"]["post"]
    assert operation["operationId"] == "createJob"
    reference = operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    body = spec["components"]["schemas"][reference.rsplit("/", 1)[-1]]
    assert set(body["required"]) == {"file", "schema"}
    assert set(body["properties"]) == {"file", "schema", "webhook_url"}
    assert spec["paths"]["/jobs/{job_id}"]["get"]["operationId"] == "getJob"


def test_upload_filename_cannot_escape_storage(client):
    response = client.post("/jobs", files={"file": ("../../outside.pdf", b"test")},
                           data={"schema": json.dumps(SCHEMA)})
    assert response.status_code == 202
    with client.session_factory() as session:
        job = session.get(Job, response.json()["job_id"])
        assert Path(job.file_path).parent == client.upload_dir
        assert Path(job.file_path).name == job.job_id + ".pdf"


def test_database_failure_cleans_uncommitted_file(client, monkeypatch):
    from app.main import app
    def fail():
        raise OSError("synthetic failure")
    monkeypatch.setattr(app.state, "session_factory", fail)
    response = submit(client)
    assert response.status_code == 503
    assert list(client.upload_dir.iterdir()) == []


@pytest.mark.parametrize("state", ["SUCCESS", "PARTIAL", "FAILED"])
def test_postgres_terminal_states_are_immutable(client, postgres_url, state):
    if postgres_url is None:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL migration and trigger checks")
    identifier = submit(client).json()["job_id"]
    with client.session_factory() as session:
        job = session.get(Job, identifier)
        job.status = "RUNNING"
        session.flush()
        job.status = state
        session.commit()
        job.status = "RUNNING"
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()
        assert session.get(Job, identifier).status == state
