import json
import logging
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.extraction import ExtractionFailure, FailureCode
from app.llm.client import Gateway
from app.llm.config import LLMConfig
from app.llm.limiter import provider_slot
from app.logging import JsonFormatter


@pytest.mark.parametrize('failure', [408, 429, 500, 502, 503, 504, 'timeout', 'connection'])
def test_transient_budget(failure):
    def respond(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('secret response')
        if failure == 'connection':
            raise httpx.ConnectError('secret endpoint')
        return httpx.Response(failure)
    sleeps = []
    gateway = Gateway(LLMConfig('https://mock.invalid', 'test'), transport=httpx.MockTransport(respond),
                      sleep=sleeps.append, jitter=lambda low, high: high / 2)
    with pytest.raises(ExtractionFailure) as caught:
        gateway.request([])
    assert caught.value.retryable
    assert len(gateway.calls) == 3 and sleeps == [0.5, 1]
    gateway.close()


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422, 501, 505])
def test_permanent_no_retry(status):
    gateway = Gateway(LLMConfig('https://mock.invalid', 'test'),
                      transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    with pytest.raises(ExtractionFailure) as caught:
        gateway.request([])
    assert not caught.value.retryable and len(gateway.calls) == 1
    gateway.close()


@pytest.mark.parametrize('header', ['65', 'date'])
def test_retry_after_not_capped(header):
    if header == "date":
        header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90))
    sleeps = []
    responses = [httpx.Response(429, headers={'Retry-After': header}),
                 httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})]
    gateway = Gateway(LLMConfig('https://mock.invalid', 'test'),
                      transport=httpx.MockTransport(lambda request: responses.pop(0)), sleep=sleeps.append)
    assert gateway.request([]) == '{}'
    assert sleeps[0] > 30
    gateway.close()


def test_retry_after_deadline_and_malformed_budget():
    for status, body, code in [(429, {}, FailureCode.DEADLINE_EXCEEDED),
                               (200, {'choices': [{'message': {'content': 'bad'}}]}, FailureCode.LLM_INVALID_RESPONSE)]:
        gateway = Gateway(LLMConfig('https://mock.invalid', 'test'), deadline=time.time() + 5,
                          transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body, headers={'Retry-After': '60'})))
        with pytest.raises(ExtractionFailure) as caught:
            gateway.json('system', {}, lambda value: None)
        assert caught.value.code == code and not caught.value.retryable
        assert len(gateway.calls) == (1 if status == 429 else 2)
        gateway.close()


def hold_slot(sender):
    with provider_slot(1, lambda: 10):
        sender.send(True)
        time.sleep(30)


def test_slot_exclusion_and_crash_release(tmp_path, monkeypatch):
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path))
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe()
    child = context.Process(target=hold_slot, args=(sender,))
    child.start()
    assert receiver.poll(10) and receiver.recv()
    with ThreadPoolExecutor() as pool:
        def acquire():
            with provider_slot(1, lambda: 10):
                return True
        pending = pool.submit(acquire)
        time.sleep(0.2)
        assert not pending.done()
        child.kill()
        child.join(5)
        assert pending.result(timeout=3)
    receiver.close()
    sender.close()
    child.close()


def test_log_redaction(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'top-secret')
    record = logging.LogRecord('test', logging.ERROR, __file__, 1,
                               'top-secret Bearer token https://private.invalid/key', (), None)
    record.job_id = 'job-1'
    record.stage = 'EXTRACTING'
    record.duration_ms = 10
    output = JsonFormatter().format(record)
    assert 'top-secret' not in output and 'private.invalid' not in output and 'Bearer token' not in output
    assert json.loads(output)['job_id'] == 'job-1'


def test_final_request_retry_preserves_provider_delay():
    gateway = Gateway(LLMConfig('https://mock.invalid', 'test', transport_retries=0),
                      transport=httpx.MockTransport(lambda request: httpx.Response(503, headers={'Retry-After': '75'})))
    with pytest.raises(ExtractionFailure) as caught:
        gateway.request([])
    assert caught.value.retryable and caught.value.retry_after == 75
    gateway.close()


@pytest.mark.parametrize('body', [[], {'choices': [None]}, {'choices': ['bad']}, {'choices': []}])
def test_malformed_envelope_uses_bounded_repair(body):
    gateway = Gateway(LLMConfig('https://mock.invalid', 'test'),
                      transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    with pytest.raises(ExtractionFailure) as caught:
        gateway.json('system', {}, lambda value: None)
    assert caught.value.code == FailureCode.LLM_INVALID_RESPONSE
    assert len(gateway.calls) == 2
    gateway.close()
