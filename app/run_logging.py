"""Run-scoped transcript logging for simulations and local agent tests.

Unlike the general API audit log, run transcripts intentionally retain message
text. They are assessment/test artefacts and must never be enabled for real
customer production data without a retention and privacy policy.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUN_LOG_DIR = ROOT / "logs" / "runs"
RUN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{8,80}$")


def validate_run_id(run_id: str | None) -> str | None:
    """Return a safe run ID or ``None``; never use an unchecked value as a path."""
    if not run_id or not RUN_ID_PATTERN.fullmatch(run_id):
        return None
    return run_id


def create_run(*, source: str, scenario_id: str | None = None) -> str:
    """Create a unique run transcript and write its first lifecycle event."""
    run_id = f"run_{uuid.uuid4().hex}"
    append_run_event(
        run_id,
        "run_started",
        source=source,
        scenario_id=scenario_id,
    )
    return run_id


def run_log_path(run_id: str) -> Path:
    """Return the log file path only for a validated run ID."""
    valid_run_id = validate_run_id(run_id)
    if not valid_run_id:
        raise ValueError("run_id must contain only letters, numbers, underscores, or hyphens.")
    return RUN_LOG_DIR / f"{valid_run_id}.jsonl"


def append_run_event(run_id: str | None, event_type: str, **fields: Any) -> None:
    """Append one chronological event; logging failures never break the workflow."""
    valid_run_id = validate_run_id(run_id)
    if not valid_run_id:
        return
    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": valid_run_id,
        "event_type": event_type,
        **{key: value for key, value in fields.items() if value is not None},
    }
    try:
        path = run_log_path(valid_run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as log_file:
            log_file.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        pass
    else:
        # Keep JSONL as the source of truth, then best-effort fan out the same
        # event to local dashboard clients. A dashboard issue must never block
        # a conversation, webhook, or evaluation.
        try:
            from app.dashboard_events import publish_run_event

            publish_run_event(event)
        except Exception:
            pass


def read_run_events(run_id: str) -> list[dict[str, Any]]:
    """Read a completed or in-progress run transcript for the future dashboard."""
    path = run_log_path(run_id)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # A concurrent writer may leave a partially written final line.
            continue
    return events
