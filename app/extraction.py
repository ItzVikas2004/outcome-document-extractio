"""Production extraction boundary. No fake data or model calls live here."""
from dataclasses import dataclass
from enum import StrEnum


class FailureCode(StrEnum):
    EXTRACTION_NOT_CONFIGURED = "EXTRACTION_NOT_CONFIGURED"
    EXTRACTION_ERROR = "EXTRACTION_ERROR"
    INVALID_RESULT = "INVALID_RESULT"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    WORKER_LOST = "WORKER_LOST"


SAFE_MESSAGES = {
    FailureCode.EXTRACTION_NOT_CONFIGURED: "Document extraction is not configured.",
    FailureCode.EXTRACTION_ERROR: "Extraction encountered a temporary failure.",
    FailureCode.INVALID_RESULT: "Extraction returned an invalid result.",
    FailureCode.ATTEMPTS_EXHAUSTED: "The maximum number of processing attempts was reached.",
    FailureCode.DEADLINE_EXCEEDED: "The document processing deadline was reached.",
    FailureCode.WORKER_LOST: "The previous processing attempt was interrupted.",
}


class ExtractionFailure(Exception):
    def __init__(self, code=FailureCode.EXTRACTION_ERROR, retryable=True):
        self.code = FailureCode(code)
        self.retryable = retryable
        super().__init__(SAFE_MESSAGES[self.code])


@dataclass
class ExtractionResult:
    result: dict
    status: str = "SUCCESS"


def extract(lease):
    raise ExtractionFailure(FailureCode.EXTRACTION_NOT_CONFIGURED, retryable=False)
