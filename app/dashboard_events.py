"""Process-local live-event broker for the development monitoring dashboard.

JSONL remains the durable transcript. This broker only distributes the same
safe run events to connected browser clients while this FastAPI process lives.
It deliberately uses thread-safe queues because simulation work and webhook
requests run outside the async SSE request coroutine.
"""

from __future__ import annotations

import contextvars
import queue
import re
import threading
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator


_current_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("dashboard_job_id", default=None)
_EMAIL_PATTERN = re.compile(r"\b([A-Z0-9._%+-])[A-Z0-9._%+-]*@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
_MAX_HISTORY_PER_JOB = 800


def _redact_text(value: str) -> str:
    """Mask e-mail addresses before an event leaves the server for the UI."""
    return _EMAIL_PATTERN.sub(lambda match: f"{match.group(1)}***@{match.group(2)}", value)


def safe_dashboard_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return UI-safe event data without raw request payloads or credentials."""
    safe = deepcopy(event)
    for field in ("text", "reason", "message", "rationale", "expected_effect"):
        value = safe.get(field)
        if isinstance(value, str):
            safe[field] = _redact_text(value)
    # These values are not useful on the dashboard and could expose how an
    # agent branch is addressed externally.
    safe.pop("agent_branch_id", None)
    return safe


class DashboardEventBroker:
    """Route run events to one live dashboard job and its SSE subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, dict[str, queue.Queue[dict[str, Any]]]] = defaultdict(dict)
        self._history: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=_MAX_HISTORY_PER_JOB))
        self._run_jobs: dict[str, str] = {}

    def publish_run_event(self, event: dict[str, Any]) -> None:
        run_id = event.get("run_id")
        if not isinstance(run_id, str):
            return
        job_id = _current_job_id.get()
        with self._lock:
            if job_id:
                self._run_jobs[run_id] = job_id
            else:
                job_id = self._run_jobs.get(run_id)
        if job_id:
            self.publish_job_event(job_id, {"kind": "run", "event": safe_dashboard_event(event)})

    def publish_job_event(self, job_id: str, payload: dict[str, Any]) -> None:
        """Publish one UI-only lifecycle event and retain it for late SSE joins."""
        event = {"job_id": job_id, **payload}
        with self._lock:
            self._history[job_id].append(event)
            subscribers = list(self._subscribers[job_id].values())
        for subscriber in subscribers:
            subscriber.put(event)

    def subscribe(self, job_id: str) -> tuple[str, queue.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """Atomically capture replay history and subscribe for later events."""
        subscriber_id = f"subscriber-{uuid.uuid4().hex}"
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            history = list(self._history.get(job_id, ()))
            self._subscribers[job_id][subscriber_id] = subscriber
        return subscriber_id, subscriber, history

    def unsubscribe(self, job_id: str, subscriber_id: str) -> None:
        with self._lock:
            self._subscribers.get(job_id, {}).pop(subscriber_id, None)

    def events_for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history.get(job_id, ()))


BROKER = DashboardEventBroker()


@contextmanager
def dashboard_job_context(job_id: str) -> Iterator[None]:
    """Associate newly created scenario runs with the dashboard job in this thread."""
    token = _current_job_id.set(job_id)
    try:
        yield
    finally:
        _current_job_id.reset(token)


def publish_run_event(event: dict[str, Any]) -> None:
    """Best-effort bridge called after a run event is durably appended."""
    BROKER.publish_run_event(event)
