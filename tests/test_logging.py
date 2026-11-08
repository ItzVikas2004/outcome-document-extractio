import json
import logging

from app.logging import JsonFormatter


def test_log_is_single_json_line():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello %s", ("world\nnext",), None)
    output = JsonFormatter().format(record)
    assert "\n" not in output
    payload = json.loads(output)
    assert payload["message"] == "hello world\nnext"
    assert payload["level"] == "INFO"
    assert payload["timestamp"].endswith("+00:00")
