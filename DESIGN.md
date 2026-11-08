# Design notes — Software Developer

The current implementation covers durable job submission and status retrieval.
Extraction, worker claiming, retry scheduling and webhook delivery are still
pending. No models have been called and no LLM budget has been spent by this code.

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
submission; actual format parsing belongs to the future worker. Field lists
require names and descriptions while allowing arbitrary field keys and extra
hints. Error responses use the contract's code/message shape. Result is omitted
until SUCCESS or PARTIAL. A PostgreSQL trigger prevents terminal rows from being
modified, including through direct SQL.

Compose runs a one-shot Alembic service after PostgreSQL becomes healthy. API and
worker start only after migrations succeed, and share the document volume. The
existing passwordless database configuration is limited to local development.

Tests exercise real multipart requests, exact size boundaries, response latency,
cross-tenant isolation, concurrent idempotency and PostgreSQL terminal-state
enforcement. PostgreSQL tests migrate a fresh isolated schema. Next steps are
leased worker claims and crash recovery, bounded provider retries, format parsers,
output validation, webhook delivery and reconciliation of orphaned files.
