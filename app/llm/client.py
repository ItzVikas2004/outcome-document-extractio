import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4

import httpx

from app.extraction import ExtractionFailure, FailureCode
from app.llm.usage import usage_values, summarize


class InvalidResponse(ValueError):
    pass


def strict_json(text):
    def constant(value):
        raise InvalidResponse()
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise InvalidResponse()
            output[key] = value
        return output
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3]
    try:
        return json.loads(text, parse_constant=constant, object_pairs_hook=pairs)
    except (ValueError, TypeError, RecursionError):
        raise InvalidResponse() from None


class Gateway:
    def __init__(self, config, *, transport=None, usage_sink=None, deadline=None, sleep=time.sleep):
        self.config, self.usage_sink, self.deadline, self.sleep = config, usage_sink, deadline, sleep
        self.calls = []
        self.input_budget = 0
        self.client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def remaining(self):
        remaining = self.deadline - time.time() if self.deadline is not None else 1800
        if remaining <= 0:
            raise ExtractionFailure(FailureCode.DEADLINE_EXCEEDED, retryable=False)
        return remaining

    def emit(self, event):
        if self.usage_sink and not self.usage_sink(event):
            raise ExtractionFailure(FailureCode.EXTRACTION_BUDGET_EXCEEDED, retryable=False)

    def request(self, messages):
        # UTF-8 byte length + framing is a conservative token upper bound, not usage.
        bound = len(json.dumps(messages, ensure_ascii=False).encode()) + 512
        config = self.config
        for attempt in range(config.transport_retries + 1):
            if (bound > config.max_input_tokens or self.input_budget + bound > config.total_input_budget
                    or len(self.calls) >= config.max_calls):
                raise ExtractionFailure(FailureCode.EXTRACTION_BUDGET_EXCEEDED, retryable=False)
            timeout = min(config.timeout_seconds, self.remaining())
            call = {"id": str(uuid4()), "model": config.model, "state": "started", "input_bound": bound}
            self.emit({"kind": "start", **call})  # Durable acknowledgement before network I/O.
            self.calls.append(call)
            self.input_budget += bound
            status, payload, delay = None, {}, min(2 ** attempt, 30)
            try:
                with self.client.stream("POST", config.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + config.api_key},
                    json={"model": config.model, "messages": messages, "temperature": 0,
                          "max_tokens": config.max_output_tokens, "response_format": {"type": "json_object"}},
                    timeout=timeout) as response:
                    status = response.status_code
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 2 * 1024 * 1024:
                            raise InvalidResponse()
                    try:
                        payload = strict_json(content.decode("utf-8"))
                    except (InvalidResponse, UnicodeError):
                        payload = {}
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(0, float(retry_after))
                        except ValueError:
                            try:
                                delay = max(0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                            except (ValueError, TypeError):
                                pass
            except httpx.TransportError:
                pass
            finally:
                call.update(usage_values(payload, config), state="completed", http_status=status)
                self.emit({"kind": "complete", **call})
            if status == 200:
                try:
                    choice = payload["choices"][0]
                    if choice.get("finish_reason") not in ("stop", None):
                        raise InvalidResponse()
                    content = choice["message"]["content"]
                    if not isinstance(content, str):
                        raise InvalidResponse()
                    return content
                except (KeyError, IndexError, TypeError):
                    raise InvalidResponse() from None
            if status is not None and status not in (408, 429) and status < 500:
                raise ExtractionFailure(FailureCode.LLM_PROVIDER_ERROR, retryable=False)
            if attempt == config.transport_retries:
                raise ExtractionFailure(FailureCode.LLM_PROVIDER_ERROR, retryable=True)
            delay = min(delay, 30)
            if delay >= self.remaining():
                raise ExtractionFailure(FailureCode.DEADLINE_EXCEEDED, retryable=False)
            self.sleep(delay)

    def json(self, system, payload, validator):
        for repair in range(self.config.repair_retries + 1):
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
            if repair:
                messages.append({"role": "user", "content": "The previous response was invalid or omitted source rows. Return complete JSON matching the specified shape and coverage rules."})
            try:
                value = strict_json(self.request(messages))
                validator(value)
                return value
            except (InvalidResponse, ValueError, KeyError, TypeError):
                if repair == self.config.repair_retries:
                    raise ExtractionFailure(FailureCode.LLM_INVALID_RESPONSE, retryable=False) from None

    def metadata(self):
        return summarize(self.calls)
