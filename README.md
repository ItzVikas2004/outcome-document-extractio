# Outcome Document Extraction Service

Role: Software Developer

Service infrastructure for extracting structured information from
PDF, DOCX, XLSX and CSV documents with arbitrary field lists.

Requires Docker Engine with Docker Compose v2:

```sh
docker compose up --build
curl http://localhost:8080/health
```

The API binds port 8080 and returns `200 {"status":"ok"}`. Compose also starts
PostgreSQL 16 with a persistent volume and a separate worker process. Both app
services wait for PostgreSQL's health check and the one-shot Alembic migration
service. The worker currently waits for a shutdown signal; processing, retries,
webhook delivery and extraction are not implemented yet. Accepted jobs remain
PENDING. There are no LLM calls. `/health` is a liveness endpoint,
not a database readiness check.

No `.env` file is required. `.env.example` documents optional configuration;
copy it to `.env` to override settings. LLM settings are intentionally empty.
The local Compose database uses trust authentication and has no published host
port. This is a development setup; configure PostgreSQL password authentication
and supply credentials through environment/secrets before deployment.

Run tests with Python 3.12 (activate the virtual environment for your shell):

```sh
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
```

Submit a document with a field list matching `contract/schema.example.json`:

```sh
curl -X POST http://localhost:8080/jobs -H 'X-Organization-Id: example' \
  -F 'file=@/path/to/document.pdf' -F 'schema=</path/to/fields.json'
curl http://localhost:8080/jobs/JOB_ID -H 'X-Organization-Id: example'
```

`POST /jobs` accepts PDF, DOCX, XLSX and CSV extensions (case insensitive), with
a file limit of 50 MiB (50 * 1024 * 1024 bytes). It validates and persists the
upload without extracting, then returns HTTP 202. Malformed/missing fields return
400; oversized files return 413. Optional `webhook_url` accepts HTTP(S) URLs and
is saved for future delivery. Arbitrary field keys and extra schema hints are
preserved. Uploaded content is not yet parsed or checked against its extension.

Use the same `X-Organization-Id` for submission and retrieval. Omitted headers
use an anonymous namespace; empty headers are rejected. Unknown and cross-tenant
jobs return 404. This header selects a namespace; it is not authentication.
Duplicate document bytes and canonicalized schema JSON within one organization
return the existing job, including its original webhook URL. Other organizations
get independent jobs. Terminal PostgreSQL rows cannot be updated.

Job records live in PostgreSQL; files live in the `document_data` Docker volume,
shared at `/service/uploads` by API and worker. Files are flushed and atomically
renamed before committing a job. Interrupted submissions can leave unreferenced
files; automatic orphan cleanup is deferred. Retrying submission is safe.

The tests use isolated SQLite databases by default. To run all tests against
PostgreSQL, set `TEST_DATABASE_URL` to a PostgreSQL SQLAlchemy URL. Tests create,
migrate and remove a unique schema, leaving application tables untouched:

```sh
docker compose run --rm -v "${PWD}/tests:/service/tests:ro" \
  -e TEST_DATABASE_URL=postgresql+psycopg://outcome@postgres:5432/outcome \
  api python -m pytest /service/tests -q
```

To run the API outside Docker, use `python -m app`; to run the worker, use
`python -m app.worker`. Both emit JSON logs to standard output. SQLAlchemy engines
are constructed without opening connections at startup. Alembic reads
`DATABASE_URL`; Compose applies migrations automatically before API/worker startup.
For a non-Docker deployment run `alembic upgrade head` before starting services.

Stop services with `docker compose down`; both data volumes are retained.

Sample validation scores: not run yet; extraction is outside this skeleton's scope.
