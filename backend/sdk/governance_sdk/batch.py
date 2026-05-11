"""BatchBuffer — thread-safe buffer that auto-flushes on size or time."""
import logging
import threading
from typing import Callable

_logger = logging.getLogger(__name__)


class BatchBuffer:
    def __init__(
        self,
        max_size: int,
        flush_interval: float,
        flush_fn: Callable[[list[dict]], None],
    ) -> None:
        self._max_size = max_size
        self._flush_fn = flush_fn
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._schedule_flush(flush_interval)

    def add(self, event: dict) -> None:
        with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= self._max_size:
                self._do_flush()

    def flush(self) -> None:
        with self._lock:
            self._do_flush()

    def _do_flush(self) -> None:
        if not self._buffer:
            return
        events, self._buffer = self._buffer[:], []
        try:
            self._flush_fn(events)
        except Exception as exc:
            _logger.warning("governance: batch flush raised unexpectedly: %s", exc)

    def _schedule_flush(self, interval: float) -> None:
        def _tick():
            with self._lock:
                self._do_flush()
            self._schedule_flush(interval)

        timer = threading.Timer(interval, _tick)
        timer.daemon = True
        timer.start()
