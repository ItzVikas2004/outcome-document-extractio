from unittest.mock import Mock

from app.worker import run_worker


def test_worker_waits_until_shutdown():
    stop_event = Mock()
    stop_event.wait.side_effect = [False, True]
    run_worker(stop_event, 0.1)
    assert stop_event.wait.call_count == 2
    stop_event.wait.assert_called_with(0.1)
