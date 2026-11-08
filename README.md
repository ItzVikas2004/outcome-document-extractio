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
service. The worker claims jobs using PostgreSQL row locks and durable leases,
renews heartbeats, retries temporary failures, and recovers abandoned attempts.
The worker parses documents and performs structured LLM extraction through your
configured gateway. Missing credentials produce `EXTRACTION_NOT_CONFIGURED`;
webhook delivery remains deferred. Fake extraction is confined to tests and is
not selectable through environment variables or uploads. `/health` is a liveness endpoint,
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
preserved. Actual document parsing happens asynchronously in the worker.

Use the same `X-Organization-Id` for submission and retrieval. Omitted headers
use an anonymous namespace; empty headers are rejected. Unknown and cross-tenant
jobs return 404. This header selects a namespace; it is not authentication.
Duplicate document bytes and canonicalized schema JSON within one organization
return the existing job, including its original webhook URL. Other organizations
get independent jobs. Terminal PostgreSQL rows cannot be updated.

Jobs transition `PENDING -> RUNNING -> SUCCESS/PARTIAL/FAILED`. Retries and crash
recovery remain RUNNING; each new attempt gets a fresh lease token. Every heartbeat,
failure and result write checks ownership and lease expiry under a row lock.
Expired workers cannot publish results or renew their leases. A database trigger
also prevents backward/skipped state transitions and changes to terminal rows.

The default lease is 30 seconds, heartbeat 5 seconds, maximum attempts 3, and retry
backoff 2, 4, ... seconds capped at 30. These are configured with the `WORKER_*`
variables in `.env.example`; the maximum attempt count is stored at submission.
`attempt_count` counts claims; `retry_count` counts attempts after the first.
First-start time, queue duration, heartbeat, completion time and the submission-based
1800-second deadline are stored internally without changing the HTTP contract.

An independent maintenance check runs while extraction is busy. A five-second
deadline reserve leaves time to persist a FAILED status; late results are rejected.
Extraction runs in a supervised subprocess so hung work can be terminated.
At least one worker and PostgreSQL must remain available to enforce deadlines;
after an outage, overdue jobs are deterministically failed on the next sweep.
External extraction side effects may repeat after a crash, but only the current
lease can publish a result. Exception logs include job ID, stage, exception type
and stack locations, excluding exception text, local variables and file contents.

Scale processing with `docker compose up -d --scale worker=3`. SIGTERM stops the
active child and lease renewal; another worker can recover after lease expiry.

Job records live in PostgreSQL; files live in the `document_data` Docker volume,
shared at `/service/uploads` by API and worker. Files are flushed and atomically
renamed before committing a job. Interrupted submissions can leave unreferenced
files; automatic orphan cleanup is deferred. Retrying submission is safe.

The tests use isolated SQLite databases by default. To run all tests against
PostgreSQL, set `TEST_DATABASE_URL` to a PostgreSQL SQLAlchemy URL. Tests create,
migrate and remove a unique schema, leaving application tables untouched:

```sh
docker compose run --rm --no-deps -v "${PWD}/tests:/service/tests:ro" \
  -e LLM_BASE_URL= -e LLM_API_KEY= \
  -e TEST_DATABASE_URL=postgresql+psycopg://outcome@postgres:5432/outcome \
  api python -m pytest /service/tests -q
```

To run the API outside Docker, use `python -m app`; to run the worker, use
`python -m app.worker`. Both emit JSON logs to standard output. SQLAlchemy engines
are constructed without opening connections at startup. Alembic reads
`DATABASE_URL`; Compose applies migrations automatically before API/worker startup.
For a non-Docker deployment run `alembic upgrade head` before starting services.

Stop services with `docker compose down`; both data volumes are retained.

Sample validation scores: not run yet. Provider tests use synthetic documents and
an in-memory httpx transport; they spend no real API credits.

Local parsers are available independently of the API/worker extraction boundary:

```python
from app.parsing import parse_document, parse_to_jsonl, render_pdf_page

parse_to_jsonl("document.xlsx", "evidence.jsonl")
for record in parse_document("document.pdf"):
    # Process one evidence record at a time; avoid list(...) on large documents.
    if record.kind == "ocr_required":
        render_pdf_page("document.pdf", record.location["page"], "fallback-page.png")
```

The internal evidence format consists of ordered `Record` objects with `kind`,
`location`, `text`, `cells` and `metadata`. All row, column, page and section
indices are one-based. Start/end records delimit documents, PDF pages, DOCX
sections/tables/header/footer regions, XLSX sheets and CSV tables. DOCX body order
is preserved, including nested tables. Paragraphs inside cells are identified by
their table/row/column location and supplement the row cells; do not count them as
additional table rows. PDF text blocks and geometrically detected tables carry
bounding boxes; PDF table recognition remains heuristic.

XLSX reads use openpyxl's read-only mode plus streaming OOXML for merged ranges,
exact raw numbers, date serials and cached formula values. Numeric values are
lexical strings with `kind="number"` internally, avoiding float precision loss.
Dates have ISO representations plus their raw serial, number format and workbook
epoch. Formula expressions and caches are preserved but never recalculated.
Merged anchor text is not copied into empty covered cells. Blank rows/cells within
the stored sheet extent are retained. CSV values remain verbatim strings (including
numeric/date text); row locations also record physical lines for multiline fields.
CSV defaults to UTF-8/BOM or BOM-marked UTF-16; optional `csv_encoding` and
`csv_delimiter` arguments handle other known input conventions.

Parsing occurs in a separate process and publishes JSONL atomically after complete
success. Defaults cap files at 50 MiB, expanded Office archives at 128 MiB, individual
XML parts at 32 MiB, shared strings at 8 MiB, records at 1 MiB, output at 128 MiB,
pages at 2,000, rows at 200,000, columns at 2,048 and cells at 2,000,000. `ParseLimits`
allows explicit changes. Docker/Linux enforces a 768 MiB address-space cap and
120-second CPU/wall timeout; non-Linux hosts retain structural limits and the wall
timeout but lack that OS memory cap. Oversized or malformed documents fail explicitly
without publishing truncated output. Empty documents raise `EMPTY_DOCUMENT`.

PDF pages without text, or predominantly raster pages with sparse text, emit
`ocr_required`; the final record reports `needs_ocr` and affected page numbers.
The fallback renderer produces a local PNG capped at 150 DPI / eight million
pixels. OCR/approved-gateway vision execution is deferred; no text is fabricated,
no hosted parser is used, and no LLM is called. DOCX pagination is not invented.
The extraction pipeline consumes these parser records. Until OCR is integrated,
documents requiring OCR fail explicitly with `DOCUMENT_REQUIRES_OCR` before any
model call, rather than silently omitting scanned pages.

Configure `LLM_BASE_URL` and `LLM_API_KEY` in the runtime environment (Compose passes
them through). The client appends `/chat/completions` to the provided base path;
there is no endpoint or key fallback. `LLM_MODEL` defaults to
`google/gemini-3.8-flash` and is restricted to the nine exact IDs in ASSIGNMENT.md.
No embeddings, hosted OCR, paid parser or alternate provider is used. Redirects
are rejected, and environment proxies are disabled for gateway calls.

The pipeline first plans field types and document/row scopes using arbitrary keys,
names, descriptions and optional `primary`. It then processes every source unit in
bounded batches with relevant header/global context. Single-document fields are
reconciled into one entry, or repeated on every table entry. Table responses must
account for every target source ID as a row, continuation, or explicit exclusion;
exclusion IDs/reasons are retained in metadata. Duplicate primary values alone never
merge rows. Only explicit, supported continuations with matching primary values
merge. Conflicts and unsupported values become null and cause PARTIAL status.
Semantic classification of logical rows still depends on the model.

Models supply evidence IDs and exact quotations. The application verifies the quote
and location against the parser, checks value support, and constructs citations.
Numbers remain JSON numbers, dates use ISO dates, and printed percentages retain
their scale. Arithmetic is recomputed from individually grounded operands. This
conservative verification can return null for legitimate paraphrases or complex
calculations; it favors unsupported-value rejection over plausible guesses.

`LLM_MAX_INPUT_TOKENS` is enforced with a conservative UTF-8 byte upper bound plus
message framing; actual usage always comes from provider usage fields. The defaults
allow 16,000 input-bound units per request, 8,192 output tokens, 500,000 total
input-bound units and 64 calls per job. HTTP retries and JSON repair count against
these budgets. Documents that cannot be fully covered fail with
`EXTRACTION_BUDGET_EXCEEDED`; no truncated table is published. Response bodies are
capped at 2 MiB. Retryable 408/429/5xx/network failures use bounded backoff and
Retry-After; malformed/omitted-row responses get bounded repair attempts.

Before each HTTP dispatch, the supervisor persists a fenced call record and
acknowledges it to the extraction child. Usage completions are idempotent and kept
across worker attempts. The final metadata aggregates `model_calls`, reported
`tokens_in`/`tokens_out`, cost and per-model totals from this ledger. Calls with
unknown outcomes after a crash remain visible as `unconfirmed_calls`, with incomplete
usage flags; their missing tokens/cost are not guessed. `model_calls` counts dispatch
attempts, including this unavoidable uncertainty window.

Provider `usage.cost` is preferred. If absent, optional `LLM_INPUT_PRICE_PER_MILLION`
and `LLM_OUTPUT_PRICE_PER_MILLION` produce explicitly labeled
`configured_rate_estimate` values for the configured model. No prices are hardcoded.
With no rate or reported cost, `cost_usd` is only the known subtotal (possibly zero),
`cost_is_known_subtotal=true`, and `cost_complete=false`; zero does not mean free.
`cost_source`, `usage_complete` and per-model flags make mixed/unknown data explicit.
