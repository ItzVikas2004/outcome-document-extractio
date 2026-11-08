# Offline API integration and load tests

From the repository root, with Python and Docker Compose available:

```sh
python loadtests/orchestrate.py
```

If Docker is not on PATH, pass `--docker` with its executable path. The host script
uses only Python's standard library; all test dependencies run in the containers.
Images/dependencies must be downloaded on the first build. Execution then uses an
internal Docker network with no internet route, no published ports, no production
environment credentials, and a separate `outcome-offline-load` project and volumes.
The synthetic placeholder credential works only with the local mock. Production
code still obtains its gateway and key from the runtime environment.

The script builds and starts the actual API, migrations, PostgreSQL and workers.
It runs ten distinct concurrent multipart CSV submissions with `asyncio.gather`
and `httpx.AsyncClient`, first with one worker and then three. Provider concurrency
is fixed at three in both runs, so that limit does not hide worker scaling. The
mock uses the existing synthetic fixture responder and delays each request by
one second. Real parsing, HTTP, grounding, queue transactions and result validation
remain active. These are small synthetic CSVs, not a large-file or accuracy benchmark.

Each POST must return 202 in under two seconds. Jobs are polled every two seconds,
with a default timeout of 1800 seconds from the start of each POST. Successful
results must validate against `contract/output.schema.json`, contain exactly the
requested fields and two logical rows, and represent a deliberately absent date
as null with an empty citation. A second GET and database snapshot two seconds
after completion must be unchanged. Database audits require one attempt, exactly
two completed model calls and no errors for every healthy batch job. This is
stronger than counting terminal responses but does not claim polling can observe
every intermediate state; the database's terminal-state invariant is also tested
in the standard integration suite.

Each scaling run additionally sends ten simultaneous duplicates with reordered
schema keys and whitespace variants. They must resolve to one job and one attempt.
Submitting identical bytes/schema in a second tenant must create a distinct job;
cross-tenant and headerless reads must return 404 in both directions.

For crash recovery the script scales down to one worker, submits another document,
waits for its durable model-call start record, sends SIGKILL to that test worker,
and starts it again. The job must finish on attempt two with WORKER_LOST history,
fenced publication and an unchanged terminal result. A replayed provider call is
expected after a crash: the test verifies one final outcome, not exactly-once model
billing. The six-second test lease keeps this check quick.

Reports in ignored `load_reports/` include individual latencies, job IDs, outcome
counts, durable attempt/call audits, p50/p95 and throughput. Percentiles use nearest
rank (p95 is the maximum for ten samples). Completion latency runs from POST start
to the first terminal observation, so includes upload, queue wait and polling
quantization. Throughput is successful jobs divided by observed batch duration;
deduplication, isolation and stability-check time are excluded from that duration.
`comparison.json` reports the actual measured ratio; no speedup is assumed or
required. Any assertion/timeout causes a nonzero exit and a failed phase report.
Run without competing test suites or workloads for a cleaner scaling comparison.

The orchestrator stops only its test stack on exit and retains volumes/reports for
diagnosis. To remove the disposable test containers and volumes yourself:

```sh
docker compose -p outcome-offline-load -f compose.load.yaml down -v
```

For a phase-only rerun while the test stack is running:

```sh
docker compose -p outcome-offline-load -f compose.load.yaml run --rm --no-deps runner python -m loadtests.run --label manual --timeout 1800
```

Load tests are outside pytest's `tests/` directory and never run in normal pytest
discovery. `tests/test_output_regression.py` adds quick deterministic checks for
missing source values versus omitted provider fields; incomplete model output
must exhaust bounded repair rather than publish a missing field.

## Observed local run (2026-11-08)

Docker Desktop, Python 3.12 containers, no competing standard test run:

| Workers | POST p50 / p95 (s) | Completion p50 / p95 (s) | Jobs/s | Successful jobs |
| --- | --- | --- | --- | --- |
| 1 | 0.208 / 0.215 | 22.454 / 44.672 | 0.224 | 10/10 |
| 3 | 0.176 / 0.188 | 10.487 / 18.556 | 0.539 | 10/10 |

Observed throughput ratio: 2.407x. Both deduplication/isolation phases passed.
SIGKILL recovery passed on attempt two with 12.877 seconds from submission to
terminal observation. All POSTs met the two-second limit. Standard suite: 150
passed, seven dependency warnings. These are measured synthetic-workload results,
not a production capacity guarantee. Raw phase reports are in `load_reports/`.
