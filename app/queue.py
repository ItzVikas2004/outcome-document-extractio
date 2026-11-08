"""PostgreSQL queue operations. Every mutation holds a short row lock."""
import copy
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import and_, func, or_, select

from app.extraction import ExtractionResult, FailureCode, SAFE_MESSAGES
from app.models import Job
from app.llm.config import ALLOWED_MODELS
from app.llm.usage import summarize

DEADLINE_MARGIN_SECONDS = 5


@dataclass(frozen=True)
class Lease:
    job_id: str
    token: str
    attempt: int
    file_path: str = field(repr=False)
    field_schema: dict = field(repr=False)
    deadline_at: datetime | None = None


@lru_cache
def result_validator():
    path = Path(__file__).resolve().parent.parent / "contract" / "output.schema.json"
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


class JobQueue:
    def __init__(self, session_factory, settings, worker_id=None):
        self.sessions = session_factory
        self.settings = settings
        self.worker_id = worker_id or str(uuid4())

    @staticmethod
    def now(session):
        return session.scalar(select(func.clock_timestamp()))

    @staticmethod
    def cutoff(job):
        return job.deadline_at - timedelta(seconds=DEADLINE_MARGIN_SECONDS)

    @staticmethod
    def clear_owner(job):
        job.lease_token = None
        job.worker_id = None
        job.lease_expires_at = None
        job.next_attempt_at = None

    def terminal_failure(self, session, job, now, code):
        # Even an unstarted timed-out job follows PENDING -> RUNNING -> FAILED.
        if job.status == "PENDING":
            job.status = "RUNNING"
            job.first_started_at = now
            job.queue_duration_ms = max(0, int((now - job.created_at).total_seconds() * 1000))
            session.flush()
        job.status = "FAILED"
        job.error = {"code": code.value, "message": SAFE_MESSAGES[code]}
        job.result = None
        job.stage = None
        job.finished_at = now
        job.updated_at = now
        self.clear_owner(job)

    def reap(self, batch_size=100):
        """Deadline/exhaustion sweep, also run while an extractor is busy."""
        with self.sessions.begin() as session:
            now = self.now(session)
            jobs = session.scalars(select(Job).where(
                Job.status.in_(("PENDING", "RUNNING")),
                or_(Job.deadline_at <= now + timedelta(seconds=DEADLINE_MARGIN_SECONDS),
                    and_(Job.status == "RUNNING", Job.attempt_count >= Job.max_attempts,
                         or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now)))
            ).order_by(Job.deadline_at, Job.job_id).limit(batch_size).with_for_update(skip_locked=True)).all()
            now = self.now(session)
            for job in jobs:
                code = FailureCode.DEADLINE_EXCEEDED if self.cutoff(job) <= now else FailureCode.ATTEMPTS_EXHAUSTED
                self.terminal_failure(session, job, now, code)
            return len(jobs)

    def claim(self):
        with self.sessions.begin() as session:
            now = self.now(session)
            job = session.scalar(select(Job).where(
                Job.attempt_count < Job.max_attempts,
                Job.deadline_at > now + timedelta(seconds=DEADLINE_MARGIN_SECONDS),
                or_(Job.status == "PENDING", and_(
                    Job.status == "RUNNING",
                    or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
                    or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now)))
            ).order_by(Job.created_at, Job.job_id).limit(1).with_for_update(skip_locked=True))
            if job is None:
                return None
            now = self.now(session)
            if now >= self.cutoff(job):
                self.terminal_failure(session, job, now, FailureCode.DEADLINE_EXCEEDED)
                return None
            job.status = "RUNNING"
            job.attempt_count += 1
            job.retry_count = job.attempt_count - 1
            job.lease_token = str(uuid4())
            job.worker_id = self.worker_id
            job.heartbeat_at = now
            job.lease_expires_at = min(now + timedelta(seconds=self.settings.worker_lease_seconds), self.cutoff(job))
            job.next_attempt_at = None
            job.updated_at = now
            job.stage = "EXTRACTING"
            job.error = None
            if job.first_started_at is None:
                job.first_started_at = now
                job.queue_duration_ms = max(0, int((now - job.created_at).total_seconds() * 1000))
            return Lease(job.job_id, job.lease_token, job.attempt_count, job.file_path, copy.deepcopy(job.field_schema), job.deadline_at)

    def owned(self, session, lease):
        job = session.scalar(select(Job).where(Job.job_id == lease.job_id).with_for_update())
        now = self.now(session)  # Sample AFTER waiting for the lock.
        if (job is None or job.status != "RUNNING" or job.lease_token != lease.token
                or job.lease_expires_at is None or job.lease_expires_at <= now):
            return None, now
        if self.cutoff(job) <= now:
            self.terminal_failure(session, job, now, FailureCode.DEADLINE_EXCEEDED)
            return None, now
        return job, now

    def heartbeat(self, lease):
        with self.sessions.begin() as session:
            job, now = self.owned(session, lease)
            if job is None:
                return False
            job.heartbeat_at = now
            job.updated_at = now
            job.lease_expires_at = min(now + timedelta(seconds=self.settings.worker_lease_seconds), self.cutoff(job))
            return True

    def fail(self, lease, code=FailureCode.EXTRACTION_ERROR, retryable=True):
        code = FailureCode(code)
        with self.sessions.begin() as session:
            job, now = self.owned(session, lease)
            if job is None:
                return False
            if not retryable:
                self.terminal_failure(session, job, now, code)
            elif job.attempt_count >= job.max_attempts:
                self.terminal_failure(session, job, now, FailureCode.ATTEMPTS_EXHAUSTED)
            else:
                delay = min(30, self.settings.worker_retry_base_seconds * 2 ** (job.attempt_count - 1))
                if now + timedelta(seconds=delay) >= self.cutoff(job):
                    self.terminal_failure(session, job, now, FailureCode.DEADLINE_EXCEEDED)
                else:
                    self.clear_owner(job)
                    job.next_attempt_at = now + timedelta(seconds=delay)
                    job.updated_at = now
                    job.stage = "RETRY_WAIT"
                    job.error = {"code": code.value, "message": SAFE_MESSAGES[code]}
            return True

    def record_usage(self, lease, event):
        """Fenced and idempotent accounting; acknowledge start before a paid request."""
        if event.get("model") not in ALLOWED_MODELS or not isinstance(event.get("id"), str):
            return False
        with self.sessions.begin() as session:
            job, now = self.owned(session, lease)
            if job is None:
                return False
            calls = copy.deepcopy(job.llm_usage)
            existing = next((call for call in calls if call["id"] == event["id"]), None)
            if event.get("kind") == "start":
                if existing:
                    return existing["lease_token"] == lease.token
                bound = event.get("input_bound")
                if (type(bound) is not int or bound <= 0 or len(calls) >= self.settings.llm_max_calls
                        or sum(c.get("input_bound", 0) for c in calls) + bound > self.settings.llm_total_input_budget):
                    return False
                calls.append({"id": event["id"], "model": event["model"], "state": "started",
                              "input_bound": bound, "lease_token": lease.token, "attempt": job.attempt_count})
            elif event.get("kind") == "complete":
                if existing is None or existing["lease_token"] != lease.token or existing["model"] != event["model"]:
                    return False
                if existing["state"] == "completed":
                    return True
                for key in ("tokens_in", "tokens_out"):
                    if event.get(key) is not None and (type(event[key]) is not int or event[key] < 0):
                        return False
                cost = event.get("cost_usd")
                if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0):
                    return False
                if event.get("cost_source") not in {"unavailable", "provider_reported", "configured_rate_estimate"}:
                    return False
                existing.update({key: event.get(key) for key in ("tokens_in", "tokens_out", "cost_usd", "cost_source")})
                existing["state"] = "completed"
            else:
                return False
            job.llm_usage = calls
            job.updated_at = now
            return True

    def finish(self, lease, output):
        # Validate outside the lock; ownership and deadline are checked afterwards.
        if not isinstance(output, ExtractionResult):
            return self.fail(lease, FailureCode.INVALID_RESULT, retryable=False)
        result = copy.deepcopy(output.result)
        valid = output.status in ("SUCCESS", "PARTIAL") and result_validator().is_valid(result)
        if valid:
            keys = set(lease.field_schema["fields"])
            valid = all(set(entry) == keys for entry in result["entries"])
            valid = valid and all(value["value"] is not None or value["citation"] == ""
                                  for entry in result["entries"] for value in entry.values())
        try:
            json.dumps(result, allow_nan=False)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            return self.fail(lease, FailureCode.INVALID_RESULT, retryable=False)
        with self.sessions.begin() as session:
            job, now = self.owned(session, lease)
            if job is None:
                return False
            result["metadata"]["latency_ms"] = max(0, int((now - job.created_at).total_seconds() * 1000))
            if job.llm_usage:
                result["metadata"].update(summarize(job.llm_usage))
            job.status = output.status
            job.result = result
            job.error = None
            job.stage = None
            job.finished_at = now
            job.updated_at = now
            self.clear_owner(job)
            return True
