"""Worker process scaffold. Durable job claiming will be implemented separately."""

import logging
import signal
from threading import Event

from app.config import Settings
from app.logging import configure_logging

logger = logging.getLogger(__name__)


def run_worker(stop_event: Event, poll_interval: float) -> None:
    logger.info("worker_started: job processing is not implemented yet")
    while not stop_event.wait(poll_interval):
        pass
    logger.info("worker_stopped")


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    stop_event = Event()

    def stop(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run_worker(stop_event, settings.worker_poll_interval_seconds)


if __name__ == "__main__":
    main()
