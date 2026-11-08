import json
import logging
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.config import Settings
from app.db import create_database_engine, create_session_factory
from app.extraction import ExtractionResult, FailureCode
from app.models import Job
from app.queue import JobQueue
from app.worker import process_lease
from tests.fakes import fake_extract, flaky_extract, hung_extract, leaking_exception, slow_extract
from tests.test_jobs import submit, SCHEMA


@pytest.fixture
def queue(client, postgres_url):
    if postgres_url is None:
        pytest.skip("PostgreSQL required for durable queue integration tests")
    settings = Settings(_env_file=None, worker_lease_seconds=10, worker_heartbeat_seconds=0.2)
    return JobQueue(client.session_factory, settings)


def row(client, identifier):
    with client.session_factory() as session:
        return session.get(Job, identifier)


def advance(client, identifier, column):
    assert column in {"lease_expires_at", "next_attempt_at"}
    with client.session_factory.begin() as session:
        session.execute(text(f"UPDATE jobs SET {column} = clock_timestamp() - interval '1 second' WHERE job_id = :id"), {"id": identifier})


def test_parallel_claims_are_unique_and_skip_locked(client, queue):
    identifiers = [submit(client, content=str(i).encode()).json()["job_id"] for i in range(12)]
    with client.session_factory.begin() as lock:
        lock.scalar(select(Job).where(Job.job_id == identifiers[0]).with_for_update())
        def claim(_):
            return JobQueue(client.session_factory, queue.settings).claim()
        with ThreadPoolExecutor(max_workers=12) as pool:
            leases = [lease for lease in pool.map(claim, range(12)) if lease]
        assert len(leases) == 11
        assert len({lease.job_id for lease in leases}) == 11
        assert identifiers[0] not in {lease.job_id for lease in leases}
    last = queue.claim()
    assert last.job_id == identifiers[0]
    assert queue.claim() is None
    for lease in leases + [last]:
        job = row(client, lease.job_id)
        assert job.attempt_count == 1 and job.retry_count == 0
        assert job.lease_token == lease.token and job.heartbeat_at is not None
        assert job.status == "RUNNING" and job.queue_duration_ms >= 0
        assert (job.deadline_at - job.created_at).total_seconds() == 1800


def test_crash_recovery_and_all_stale_writes_fenced(client, queue):
    identifier = submit(client).json()["job_id"]
    stale = queue.claim()
    advance(client, identifier, "lease_expires_at")
    assert not queue.heartbeat(stale)
    assert not queue.finish(stale, fake_extract(stale))
    fresh_queue = JobQueue(client.session_factory, queue.settings)
    fresh = fresh_queue.claim()
    assert fresh.job_id == identifier and fresh.attempt == 2 and fresh.token != stale.token
    assert not queue.fail(stale)
    assert not queue.heartbeat(stale)
    assert not queue.finish(stale, fake_extract(stale))
    assert fresh_queue.finish(fresh, fake_extract(fresh))
    snapshot = row(client, identifier)
    assert not queue.finish(stale, fake_extract(stale))
    assert row(client, identifier).updated_at == snapshot.updated_at
    assert snapshot.status == "SUCCESS" and snapshot.finished_at < snapshot.deadline_at


def claim_and_crash(url, sender):
    settings = Settings(_env_file=None, database_url=url)
    engine = create_database_engine(settings)
    queue = JobQueue(create_session_factory(engine), settings)
    sender.send(queue.claim())
    sender.close()
    os._exit(23)


def test_actual_process_crash_after_committed_claim(client, queue, postgres_url):
    identifier = submit(client).json()["job_id"]
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(False)
    process = context.Process(target=claim_and_crash, args=(postgres_url, sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(15)
        lost = receiver.recv()
        process.join(timeout=5)
        assert process.exitcode == 23
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        receiver.close()
        process.close()
    assert row(client, identifier).status == "RUNNING"
    advance(client, identifier, "lease_expires_at")
    recovered = queue.claim()
    assert recovered.attempt == 2
    assert queue.finish(recovered, fake_extract(recovered))
    assert not queue.fail(lost)


def test_crashes_are_bounded_without_losing_job(client, queue):
    identifier = submit(client).json()["job_id"]
    for attempt in range(1, 4):
        lease = queue.claim()
        assert lease.attempt == attempt
        advance(client, identifier, "lease_expires_at")
    assert queue.claim() is None
    assert queue.reap() == 1
    job = row(client, identifier)
    assert job.status == "FAILED" and job.error["code"] == "ATTEMPTS_EXHAUSTED"
    assert job.attempt_count == 3 and job.retry_count == 2
    assert queue.reap() == 0


def test_retries_backoff_and_eventual_success_in_real_worker(client, queue):
    identifier = submit(client).json()["job_id"]
    first_started = None
    for attempt in range(1, 4):
        lease = queue.claim()
        process_lease(queue, lease, Event(), queue.settings, flaky_extract)
        job = row(client, identifier)
        first_started = first_started or job.first_started_at
        assert job.first_started_at == first_started
        if attempt < 3:
            assert job.status == "RUNNING" and job.stage == "RETRY_WAIT"
            assert job.lease_token is None and job.next_attempt_at > job.updated_at
            assert queue.claim() is None
            advance(client, identifier, "next_attempt_at")
    assert job.status == "SUCCESS" and job.retry_count == 2
    assert job.result["metadata"]["latency_ms"] >= job.queue_duration_ms


def test_retry_exhaustion_and_safe_errors(client, queue, caplog):
    identifier = submit(client).json()["job_id"]
    for attempt in range(3):
        lease = queue.claim()
        with caplog.at_level(logging.ERROR):
            process_lease(queue, lease, Event(), queue.settings, leaking_exception)
        if attempt < 2:
            advance(client, identifier, "next_attempt_at")
    job = row(client, identifier)
    assert job.status == "FAILED" and job.error["code"] == "ATTEMPTS_EXHAUSTED"
    assert identifier in caplog.text and "EXTRACTING" in caplog.text and "RuntimeError" in caplog.text
    assert "api-key-secret" not in caplog.text and "document contents" not in caplog.text
    assert "api-key-secret" not in json.dumps(job.error)


def test_heartbeats_keep_long_extraction_owned(client, queue):
    identifier = submit(client).json()["job_id"]
    queue.settings = Settings(_env_file=None, worker_lease_seconds=1, worker_heartbeat_seconds=0.1)
    lease = queue.claim()
    initial = row(client, identifier)
    process_lease(queue, lease, Event(), queue.settings, slow_extract)
    job = row(client, identifier)
    assert job.status == "SUCCESS" and job.attempt_count == 1
    assert (job.heartbeat_at - initial.heartbeat_at).total_seconds() > 1


@pytest.mark.parametrize("started", [False, True])
def test_deadline_includes_waiting_and_processing(client, queue, started):
    identifier = submit(client).json()["job_id"]
    lease = queue.claim() if started else None
    with client.session_factory.begin() as session:
        session.execute(text("UPDATE jobs SET deadline_at=clock_timestamp()+interval '4 seconds' WHERE job_id=:id"), {"id": identifier})
    assert queue.reap() == 1
    job = row(client, identifier)
    assert job.status == "FAILED" and job.error["code"] == "DEADLINE_EXCEEDED"
    assert job.finished_at < job.deadline_at
    if lease:
        assert not queue.finish(lease, fake_extract(lease))


def test_hung_extractor_is_terminated_and_queue_deadlines_still_run(client, queue):
    identifier = submit(client).json()["job_id"]
    lease = queue.claim()
    waiting = submit(client, content=b"waiting").json()["job_id"]
    with client.session_factory.begin() as session:
        session.execute(text("UPDATE jobs SET deadline_at=clock_timestamp()+interval '6 seconds'"))
    before = {child.pid for child in multiprocessing.active_children()}
    started = monotonic()
    process_lease(queue, lease, Event(), queue.settings, hung_extract)
    assert monotonic() - started < 5
    assert {child.pid for child in multiprocessing.active_children()} == before
    assert row(client, identifier).status == "FAILED"
    assert row(client, waiting).status == "FAILED"


@pytest.mark.parametrize("status", ["SUCCESS", "PARTIAL", "FAILED"])
def test_terminal_rows_reject_every_mutation(client, queue, status):
    identifier = submit(client).json()["job_id"]
    lease = queue.claim()
    if status == "FAILED":
        queue.fail(lease, retryable=False)
    else:
        output = fake_extract(lease)
        output.status = status
        queue.finish(lease, output)
    for assignment in ("status='RUNNING'", "retry_count=99", "result='{}'::jsonb", "error='{}'::jsonb"):
        with pytest.raises(DBAPIError):
            with client.session_factory.begin() as session:
                session.execute(text(f"UPDATE jobs SET {assignment} WHERE job_id=:id"), {"id": identifier})
    assert not queue.heartbeat(lease) and not queue.fail(lease)
    assert row(client, identifier).status == status


def test_backward_and_skipped_state_transitions_rejected(client, queue):
    identifier = submit(client).json()["job_id"]
    with pytest.raises(DBAPIError):
        with client.session_factory.begin() as session:
            session.execute(text("UPDATE jobs SET status='SUCCESS' WHERE job_id=:id"), {"id": identifier})
    queue.claim()
    with pytest.raises(DBAPIError):
        with client.session_factory.begin() as session:
            session.execute(text("UPDATE jobs SET status='PENDING' WHERE job_id=:id"), {"id": identifier})


@pytest.mark.parametrize("kind", ["empty", "wrong_type", "wrong_fields", "null_citation", "nonfinite"])
def test_invalid_results_cannot_be_published(client, queue, kind):
    identifier = submit(client).json()["job_id"]
    lease = queue.claim()
    output = fake_extract(lease)
    if kind == "empty":
        output = ExtractionResult({"entries": [], "metadata": {}})
    elif kind == "wrong_type":
        output = {"unexpected": "object"}
    elif kind == "wrong_fields":
        output.result["entries"] = [{}]
    elif kind == "null_citation":
        output.result["entries"][0]["arbitrary_key"]["citation"] = "unsupported evidence"
    else:
        output.result["metadata"]["cost_usd"] = float("nan")
    queue.finish(lease, output)
    assert row(client, identifier).error["code"] == "INVALID_RESULT"


def test_production_extractor_never_uses_fake_result(client, queue):
    identifier = submit(client).json()["job_id"]
    lease = queue.claim()
    process_lease(queue, lease, Event(), queue.settings)
    job = row(client, identifier)
    assert job.status == "FAILED" and job.error["code"] == "EXTRACTION_NOT_CONFIGURED"
    assert job.result is None and job.attempt_count == 1


def test_dedupe_in_running_and_terminal_states_ignores_webhook(client, queue):
    first = submit(client, webhook_url="https://example.test/first").json()
    lease = queue.claim()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: submit(client, schema=json.dumps(SCHEMA, indent=2),
                                                   webhook_url="https://example.test/second"), range(8)))
    assert {r.json()["job_id"] for r in responses} == {first["job_id"]}
    queue.finish(lease, fake_extract(lease))
    assert submit(client).json()["job_id"] == first["job_id"]
    assert row(client, first["job_id"]).webhook_url == "https://example.test/first"
    assert submit(client, organization="other").json()["job_id"] != first["job_id"]


def test_shutdown_stops_child_and_leaves_recoverable_lease(client, queue):
    from threading import Timer
    identifier = submit(client).json()["job_id"]
    lease = queue.claim()
    stop = Event()
    timer = Timer(0.5, stop.set)
    before = {child.pid for child in multiprocessing.active_children()}
    timer.start()
    try:
        process_lease(queue, lease, stop, queue.settings, hung_extract)
    finally:
        timer.join()
    assert {child.pid for child in multiprocessing.active_children()} == before
    assert row(client, identifier).status == "RUNNING"
    advance(client, identifier, "lease_expires_at")
    recovered = queue.claim()
    assert recovered.token != lease.token
    assert queue.finish(recovered, fake_extract(recovered))
