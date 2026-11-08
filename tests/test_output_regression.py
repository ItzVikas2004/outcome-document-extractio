"""Contract regressions remain quick and deterministic in the standard suite."""
import json
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator

from app.extraction import ExtractionFailure, FailureCode
from app.llm.config import LLMConfig
from app.llm.pipeline import extract_document
from app.queue import Lease
from tests.mock_provider import Provider, TABLE_SCHEMA, table_response


def test_missing_source_field_is_present_as_null(tmp_path):
    path = tmp_path / 'missing.csv'
    path.write_text('Unit,Rent,Date\nA,1200.50,\nB,,2026-01-02\n')
    lease = Lease('synthetic', 'token', 1, str(path), TABLE_SCHEMA)
    result = extract_document(lease, LLMConfig('https://mock.invalid', 'test'),
                              transport=httpx.MockTransport(Provider()))
    validator = Draft202012Validator(json.loads(Path('contract/output.schema.json').read_text()))
    validator.validate(result.result)
    entries = result.result['entries']
    assert len(entries) == 2
    assert all(set(entry) == set(TABLE_SCHEMA['fields']) for entry in entries)
    for entry, key in [(entries[0], 'date_custom'), (entries[1], 'amount_custom')]:
        assert entry[key] == {'value': None, 'citation': '', 'confidence': 0}


def test_provider_omission_cannot_publish_incomplete_output(tmp_path):
    path = tmp_path / 'omitted.csv'
    path.write_text('Unit,Rent,Date\nA,1200.50,2026-01-02\n')
    def omit(payload):
        result = table_response(payload)
        del result['rows'][0]['fields']['date_custom']
        return result
    provider = Provider(extractor=omit)
    with pytest.raises(ExtractionFailure) as caught:
        extract_document(Lease('synthetic', 'token', 1, str(path), TABLE_SCHEMA),
                         LLMConfig('https://mock.invalid', 'test'), transport=httpx.MockTransport(provider))
    assert caught.value.code == FailureCode.LLM_INVALID_RESPONSE
    assert len(provider.requests) == 3  # planning plus extraction and one repair
