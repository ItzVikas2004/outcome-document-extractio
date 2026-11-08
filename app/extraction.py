"""Production extraction boundary and safe failure vocabulary."""
from dataclasses import dataclass
from enum import StrEnum


class FailureCode(StrEnum):
    EXTRACTION_NOT_CONFIGURED = "EXTRACTION_NOT_CONFIGURED"
    EXTRACTION_ERROR = "EXTRACTION_ERROR"
    INVALID_RESULT = "INVALID_RESULT"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    WORKER_LOST = "WORKER_LOST"
    LLM_CONFIGURATION = "LLM_CONFIGURATION"
    LLM_PROVIDER_ERROR = "LLM_PROVIDER_ERROR"
    LLM_INVALID_RESPONSE = "LLM_INVALID_RESPONSE"
    EXTRACTION_BUDGET_EXCEEDED = "EXTRACTION_BUDGET_EXCEEDED"
    DOCUMENT_REQUIRES_OCR = "DOCUMENT_REQUIRES_OCR"
    DOCUMENT_PARSE_FAILED = "DOCUMENT_PARSE_FAILED"


SAFE_MESSAGES = {
    FailureCode.EXTRACTION_NOT_CONFIGURED: "Document extraction is not configured.",
    FailureCode.EXTRACTION_ERROR: "Extraction encountered a temporary failure.",
    FailureCode.INVALID_RESULT: "Extraction returned an invalid result.",
    FailureCode.ATTEMPTS_EXHAUSTED: "The maximum number of processing attempts was reached.",
    FailureCode.DEADLINE_EXCEEDED: "The document processing deadline was reached.",
    FailureCode.WORKER_LOST: "The previous processing attempt was interrupted.",
    FailureCode.LLM_CONFIGURATION: "The configured language model or gateway is invalid.",
    FailureCode.LLM_PROVIDER_ERROR: "The language model gateway could not complete the request.",
    FailureCode.LLM_INVALID_RESPONSE: "The language model returned an invalid or incomplete response.",
    FailureCode.EXTRACTION_BUDGET_EXCEEDED: "The extraction budget cannot cover the complete document.",
    FailureCode.DOCUMENT_REQUIRES_OCR: "The document requires OCR before grounded extraction.",
    FailureCode.DOCUMENT_PARSE_FAILED: "The document could not be parsed within resource limits.",
}


class ExtractionFailure(Exception):
    def __init__(self, code=FailureCode.EXTRACTION_ERROR, retryable=True, retry_after=0):
        self.code = FailureCode(code)
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(SAFE_MESSAGES[self.code])


@dataclass
class ExtractionResult:
    result: dict
    status: str = "SUCCESS"


def extract(lease, usage_sink=None):
    from app.llm.config import LLMConfig
    from app.llm.pipeline import extract_document
    config = LLMConfig.from_env()
    return extract_document(lease, config, usage_sink=usage_sink)
