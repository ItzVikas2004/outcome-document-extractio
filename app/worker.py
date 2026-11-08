"""Lease supervisor; extraction runs in a killable child with no database session."""
import json
import inspect
import logging
import multiprocessing
import os
from pathlib import Path
import signal
import sys
from threading import Event
from time import monotonic
import traceback

from app.config import Settings
from app.db import create_database_engine, create_session_factory
from app.extraction import ExtractionFailure, FailureCode, extract
from app.logging import configure_logging
from app.queue import JobQueue

logger = logging.getLogger(__name__)


def safe_exception(exc):
    # No exception messages, source lines, locals, SQL or document data.
    frames = traceback.extract_tb(exc.__traceback__)
    return {"type": type(exc).__name__, "frames": [
        {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for frame in frames]}


def extraction_child(connection, extractor, lease, parent_pid):
    # Container shutdown kills the process tree; also handle a supervisor-only
    # SIGKILL on Linux so hung extractors cannot survive their parent indefinitely.
    if sys.platform == "linux":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
            os._exit(1)
    logging.disable(logging.CRITICAL)
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        try:
            def usage_sink(event):
                connection.send(("usage", event))
                return connection.recv()
            options = {"usage_sink": usage_sink} if "usage_sink" in inspect.signature(extractor).parameters else {}
            connection.send(("result", extractor(lease, **options)))
        except Exception as exc:
            code = exc.code if isinstance(exc, ExtractionFailure) else FailureCode.EXTRACTION_ERROR
            retryable = exc.retryable if isinstance(exc, ExtractionFailure) else True
            connection.send(("error", (code, retryable, safe_exception(exc))))
        finally:
            connection.close()


def process_lease(queue, lease, stop_event, settings, extractor=extract):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=True)
    child = context.Process(target=extraction_child, args=(sender, extractor, lease, os.getpid()), daemon=True)
    child.start()
    sender.close()
    next_heartbeat = monotonic()
    next_maintenance = monotonic()
    try:
        while not stop_event.is_set():
            if monotonic() >= next_maintenance:
                if queue.reap():
                    next_heartbeat = monotonic()
                next_maintenance = monotonic() + min(1, settings.worker_poll_interval_seconds)
            if monotonic() >= next_heartbeat:
                if not queue.heartbeat(lease):
                    logger.info("lease_lost job_id=%s stage=EXTRACTING", lease.job_id)
                    return
                next_heartbeat = monotonic() + settings.worker_heartbeat_seconds
            if receiver.poll(min(0.1, settings.worker_heartbeat_seconds)):
                try:
                    kind, payload = receiver.recv()
                except EOFError:
                    queue.fail(lease, FailureCode.WORKER_LOST)
                    return
                if kind == "usage":
                    receiver.send(queue.record_usage(lease, payload))
                    continue
                if kind == "result":
                    published = queue.finish(lease, payload)
                    logger.info("job_completion job_id=%s stage=VERIFYING accepted=%s", lease.job_id, published)
                else:
                    code, retryable, details = payload
                    logger.error("extraction_failed job_id=%s stage=EXTRACTING details=%s", lease.job_id, json.dumps(details))
                    queue.fail(lease, code, retryable)
                return
            if not child.is_alive():
                queue.fail(lease, FailureCode.WORKER_LOST)
                return
        # Stop renewing on shutdown; a replacement recovers after lease expiry.
    finally:
        if child.is_alive():
            child.terminate()
        child.join(timeout=1)
        if child.is_alive():
            child.kill()
            child.join(timeout=1)
        receiver.close()
        child.close()


def run_worker(queue, stop_event, settings, extractor=extract):
    logger.info("worker_started worker_id=%s", queue.worker_id)
    while not stop_event.is_set():
        lease = None
        try:
            queue.reap()
            lease = queue.claim()
            if lease is not None:
                logger.info("job_claimed job_id=%s stage=EXTRACTING attempt=%s", lease.job_id, lease.attempt)
                process_lease(queue, lease, stop_event, settings, extractor)
                continue
        except Exception as exc:
            logger.error("worker_error job_id=%s stage=%s details=%s",
                         lease.job_id if lease else "none", "EXTRACTING" if lease else "CLAIMING",
                         json.dumps(safe_exception(exc)))
            # Unknown DB commit outcomes are recovered by persisted leases.
        stop_event.wait(settings.worker_poll_interval_seconds)
    logger.info("worker_stopped worker_id=%s", queue.worker_id)


def main():
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_database_engine(settings)
    queue = JobQueue(create_session_factory(engine), settings)
    stop_event = Event()
    def stop(signum, frame):
        stop_event.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        run_worker(queue, stop_event, settings)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
