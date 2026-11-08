from unittest.mock import Mock
from threading import Event

from app.config import Settings

from app.worker import run_worker


def test_worker_waits_until_shutdown():
    stop_event = Event()
    queue = Mock()
    queue.claim.side_effect = lambda: stop_event.set()
    run_worker(queue, stop_event, Settings(_env_file=None, worker_poll_interval_seconds=0.01))
    queue.reap.assert_called_once()
    queue.claim.assert_called_once()
