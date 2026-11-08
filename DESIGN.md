# Design notes — Software Developer

The current implementation covers durable job submission and status retrieval.
The worker uses durable leases, retries and ownership fencing and now performs
grounded structured extraction. The default model is google/gemini-3.8-flash, chosen
from the assignment allowlist; model choice is configurable only within that list.
The gateway and key are read from the environment at execution time. No real calls
were made during implementation: tests use a mock httpx transport, spending $0.
Webhook delivery and OCR/vision transcription remain deferred.

PostgreSQL is the source of truth for jobs. The API validates multipart input,
writes the document, computes SHA-256 hashes and commits a PENDING job. It performs
no extraction on the request path. SQLAlchemy sessions are scoped to individual
requests; blocking database and file work runs in FastAPI's thread pool.

Documents use server-generated UUID filenames in a shared persistent volume.
The API flushes file contents, atomically renames the file and, on Linux, flushes
the directory before committing the row. This ordering prevents acknowledged
jobs from referencing files that have not yet been published. Filesystem and
database transactions are not atomic together: a crash before the database commit,
or an uncertain commit outcome, may leave an unreferenced file. Cleanup should
use a grace period and database reconciliation; it must never delete a file
belonging to a committed job. That maintenance process is not implemented yet.

Idempotency uses a database unique constraint on organization, document hash and
canonical schema hash. Concurrent duplicate submissions resolve to the winning
row and discard their extra files. JSON object key order and whitespace do not
change the schema hash; array order and field content do. The first submission's
webhook URL is retained. No deduplication occurs across organizations.

Omitting X-Organization-Id selects an anonymous namespace. Empty headers are
rejected. The same namespace predicate is applied to every read and duplicate
lookup. The header alone is not authentication; authenticated tenant identity
would be required in a deployed multi-user service.

The file size limit is interpreted as 50 MiB. Only extensions are checked at
submission; actual format parsing happens in the worker. Field lists
require names and descriptions while allowing arbitrary field keys and extra
hints. Error responses use the contract's code/message shape. Result is omitted
until SUCCESS or PARTIAL. A PostgreSQL trigger prevents terminal rows from being
modified, including through direct SQL.

Compose runs a one-shot Alembic service after PostgreSQL becomes healthy. API and
worker start only after migrations succeed, and share the document volume. The
existing passwordless database configuration is limited to local development.

Workers atomically select eligible rows with FOR UPDATE SKIP LOCKED and commit
RUNNING plus an attempt count, random fencing token, heartbeat and lease expiry.
Transactions are short; extraction runs outside the database transaction in a
supervised child process. Heartbeats and writes lock the row, then compare its
token, state, lease expiry and deadline using PostgreSQL time. An expired token
cannot be renewed. Retries and recovered claims remain RUNNING, preserving the
forward-only public state machine. Claims increment attempt_count; retry_count is
attempt_count minus one. A bounded exponential delay is persisted for retryable
errors. Crashed attempts become eligible after lease expiry; exhausting the stored
attempt budget terminates the job. Child output is validated against the output
contract and requested field keys before a fenced publication transaction.

Submission time and the 1800-second deadline are assigned by PostgreSQL. The first
claim records queue duration; completion latency includes waiting and retries.
Workers sweep deadlines while idle and while supervising extraction, reserving five
seconds to persist terminal failures. Hung children are killed on lost ownership,
deadline expiry or shutdown. Database connection, lock and statement timeouts bound
normal stalls. A total database/worker outage necessarily prevents on-time writes;
the next surviving worker deterministically fails overdue jobs rather than reviving
them. Processing is at least once; result publication is fenced and terminal.

Safe failures have allowlisted codes/messages. Diagnostic logs record exception
types and stack locations but omit raw exception text, source lines, SQL arguments,
locals and document contents. Child stdout/stderr and library logging are suppressed.

Tests use a freshly migrated PostgreSQL schema and cover concurrent claims/dedupe,
an actual process crash after claim commit, retries, lease expiry/renewal, stale
writes, deadline sweeps, hung children and database state invariants. Next steps
are OCR/vision integration, webhook delivery, and orphan reconciliation.

Local parsing is now implemented independently in `app/parsing`, using PyMuPDF,
python-docx, openpyxl and Python csv. An isolated process writes ordered JSONL
evidence to disk before atomic publication, bounding address space on Linux,
execution time, expanded archives, rows/cells, individual records and total output.
CSV and XLSX are row-oriented; DOCX's in-memory package is bounded by archive/XML
limits and the process memory cap. Read-only openpyxl is supplemented with streaming
XML because its numeric conversion loses lexical precision and its read-only
worksheet omits merged-cell metadata. Raw numeric/date/formula evidence survives;
CSV/date text is never guessed into a numeric type. PDF table grouping is geometric
and may require later vision assistance. Textless/sparse scanned pages produce
explicit OCR-required records and can be rendered with a bounded local PNG helper.
Actual OCR/vision integration remains deferred. Synthetic test fixtures are generated
at runtime; no sample documents or answers are copied into the repository.

Extraction indexes evidence in a temporary SQLite database, plans types/scopes, then
processes all source units in bounded batches. Context selection uses local lexical
overlap and table headings, without embeddings. Every table unit must be explicitly
accounted for; omitted units cause repair/failure. Logical continuation requires an
explicit prior source ID and matching supported primary value; duplicate keys alone
never collapse rows. Global facts are propagated to every entry; conflicting claims
become null. Semantic row classification remains a model decision and needs sample
evaluation. Budget exhaustion fails explicitly instead of returning a truncated table.

The provider response is an internal evidence-bearing structure, not trusted final
JSON. Quotes must be exact parser substrings; locations are generated locally.
Scalar/date/percentage support and grounded arithmetic are checked before publication.
Unsupported fields become null with PARTIAL status. This trades some recall (notably
paraphrases and unsupported calculations) for more reliable citations and null discipline.

Dispatches are durably recorded before network I/O using supervisor acknowledgements;
completions and aggregation are fenced by lease token. Unknown crash outcomes remain
incomplete rather than gaining invented tokens or cost. Provider cost wins over optional
configured-rate estimates; absent both, metadata explicitly labels a known subtotal.
Transport retries and JSON repairs consume the same persisted per-job call/input budget.
With another week, priorities are measured sample accuracy, table-boundary/continuation
evaluation, OCR integration and resumable chunk checkpoints to avoid repeat calls after
a crash. Actual model quality and price were not validated with live requests here.
