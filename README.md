# Outcome Document Extraction Service

Role: Software Developer

Initial skeleton for a service that will extract structured information from
PDF, DOCX, XLSX and CSV documents with arbitrary field lists.

Requires Docker Engine with Docker Compose v2:

```sh
docker compose up --build
curl http://localhost:8080/health
```

The API binds port 8080 and returns `200 {"status":"ok"}`. Compose also starts
PostgreSQL 16 with a persistent volume and a separate worker process. Both app
services wait for PostgreSQL's health check. The worker currently waits for a
shutdown signal; job submission, durable processing, retries and extraction are
not implemented yet. There are no LLM calls. `/health` is a liveness endpoint,
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

To run the API outside Docker, use `python -m app`; to run the worker, use
`python -m app.worker`. Both emit JSON logs to standard output. SQLAlchemy engines
are constructed lazily. Alembic is configured to read `DATABASE_URL`; there are
no application tables or migrations yet. After adding migrations, apply them with
`docker compose run --rm api alembic upgrade head` before starting workers.

Stop services with `docker compose down`; the database volume is retained.

Sample validation scores: not run yet; extraction is outside this skeleton's scope.
