"""Deterministic TEST-ONLY extraction. Not selectable via production configuration."""
import time

from app.extraction import ExtractionFailure, ExtractionResult


def fake_extract(lease):
    return ExtractionResult({
        "entries": [{key: {"value": None, "citation": "", "confidence": 0.0}
                     for key in lease.field_schema["fields"]}],
        "metadata": {"model_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0, "latency_ms": 0},
    })


def flaky_extract(lease):
    if lease.attempt < 3:
        raise ExtractionFailure()
    return fake_extract(lease)


def slow_extract(lease):
    time.sleep(2)
    return fake_extract(lease)


def hung_extract(lease):
    while True:
        time.sleep(0.1)


def leaking_exception(lease):
    raise RuntimeError("api-key-secret document contents never log me")
