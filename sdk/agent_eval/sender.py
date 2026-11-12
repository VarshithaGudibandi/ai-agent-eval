import functools
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from agent_eval.client import PlatformClient, PlatformError, batches, encode, retrying

logger = logging.getLogger("agent_eval")

_STOP = object()


class BackgroundSender:
    """Sends live traces and feedback from a background thread.

    The app only ever puts an item on a bounded queue, which never waits: when the queue is
    full, or the platform cannot be reached after a few tries, items are dropped. Live
    traffic must never slow down or fail because of the platform.
    """

    def __init__(
        self,
        connect: Callable[[], PlatformClient],
        max_queue: int = 10_000,
        linger_seconds: float = 0.5,
        attempts: int = 3,
    ) -> None:
        self._connect = connect
        self._max_queue = max_queue
        self._linger = linger_seconds
        self._attempts = attempts
        self._lock = threading.Lock()
        self._queue: queue.Queue[Any] = queue.Queue(max_queue)
        self._thread: threading.Thread | None = None
        self._pid = os.getpid()
        self._failing = False
        self.dropped = 0

    def submit_trace(self, trace: dict[str, Any]) -> None:
        self._put(("trace", trace))

    def submit_feedback(self, feedback: dict[str, Any]) -> None:
        self._put(("feedback", feedback))

    def _put(self, item: tuple[str, dict[str, Any]]) -> None:
        try:
            self._running().put_nowait(item)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                logger.warning("agent_eval: send queue full, %d items dropped", self.dropped)

    def _running(self) -> "queue.Queue[Any]":
        with self._lock:
            if self._pid != os.getpid():
                # A forked worker process does not inherit the parent's thread.
                self._queue, self._thread, self._pid = (
                    queue.Queue(self._max_queue),
                    None,
                    os.getpid(),
                )
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="agent-eval-sender", daemon=True
                )
                self._thread.start()
            return self._queue

    def close(self, timeout: float = 2.0) -> None:
        """Send what is queued, waiting at most timeout seconds."""
        thread = self._thread
        if thread is None or not thread.is_alive() or self._pid != os.getpid():
            return
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            return
        thread.join(timeout)

    def _run(self) -> None:
        client = self._connect()
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                items = [item]
                stop = self._gather(items)
                self._deliver(client, items)
                if stop:
                    return
        except Exception as exc:
            logger.warning("agent_eval: the sender stopped (%s)", type(exc).__name__)
        finally:
            client.close()

    def _gather(self, items: list[Any]) -> bool:
        """Add whatever arrives within the linger time. True if asked to stop."""
        deadline = time.monotonic() + self._linger
        while len(items) < 500:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                return False
            if item is _STOP:
                return True
            items.append(item)
        return False

    def _deliver(self, client: PlatformClient, items: list[tuple[str, dict[str, Any]]]) -> None:
        encoded = []
        for kind, body in items:
            if kind == "feedback":
                self._attempt(functools.partial(client.send_feedback, body), 1)
                continue
            try:
                encoded.append(encode(body))
            except Exception as exc:
                logger.warning("agent_eval: a trace could not be encoded (%s)", type(exc).__name__)
        for batch in batches(encoded):
            result = self._attempt(functools.partial(client.send_traces, batch), len(batch))
            for rejected in (result or {}).get("rejected", []):
                logger.warning(
                    "agent_eval: trace %s rejected: %s", rejected["trace_id"], rejected["error"]
                )

    def _attempt(self, call: Callable[[], Any], count: int) -> Any:
        try:
            result = retrying(call, self._attempts)
        except PlatformError as exc:
            self.dropped += count
            # Warn when sending starts failing, not on every batch while the server is down.
            if not self._failing:
                logger.warning("agent_eval: dropping live data, %s", exc)
            self._failing = True
            return None
        if self._failing:
            logger.warning("agent_eval: sending works again")
        self._failing = False
        return result
