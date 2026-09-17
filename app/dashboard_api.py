"""Development monitoring dashboard routes for simulation and refinement jobs."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.dashboard_events import BROKER, dashboard_job_context, safe_dashboard_event
from app.refinement_applier import load_refinement_diff
from app.refinement_runner import RefinementRunner, RefinementRunnerError
from app.run_logging import read_run_events, validate_run_id
from app.scenarios import load_scenarios


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "app" / "static"
EVALUATION_DIR = ROOT / "logs" / "evaluations"
REFINEMENT_DIR = ROOT / "logs" / "refinements"

router = APIRouter(prefix="/dashboard-api", tags=["dashboard"])


class StartDashboardJobRequest(BaseModel):
    scenario_id: str = Field(min_length=1)
    apply_changes: bool = False


@dataclass
class DashboardJob:
    job_id: str
    scenario_id: str
    apply_changes: bool
    status: str = "queued"
    started_at_utc: str | None = None
    completed_at_utc: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    _thread: threading.Thread | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        events = BROKER.events_for_job(self.job_id)
        run_ids = list(dict.fromkeys(
            event["event"].get("run_id")
            for event in events
            if event.get("kind") == "run" and isinstance(event.get("event", {}).get("run_id"), str)
        ))
        return {
            "job_id": self.job_id,
            "scenario_id": self.scenario_id,
            "apply_changes": self.apply_changes,
            "status": self.status,
            "started_at_utc": self.started_at_utc,
            "completed_at_utc": self.completed_at_utc,
            "result": self.result,
            "error": self.error,
            "run_ids": run_ids,
        }


class DashboardJobStore:
    """In-process job registry; durable per-run history remains in JSONL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, DashboardJob] = {}

    def start(self, request: StartDashboardJobRequest) -> DashboardJob:
        _, scenarios = load_scenarios()
        if request.scenario_id not in {scenario.id for scenario in scenarios}:
            raise KeyError(request.scenario_id)
        job = DashboardJob(
            job_id=f"job_{uuid.uuid4().hex}",
            scenario_id=request.scenario_id,
            apply_changes=request.apply_changes,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        thread = threading.Thread(target=self._run, args=(job,), daemon=True, name=f"dashboard-{job.job_id[-8:]}")
        job._thread = thread
        thread.start()
        return job

    def get(self, job_id: str) -> DashboardJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(self, job: DashboardJob) -> None:
        job.status = "running"
        job.started_at_utc = datetime.now(timezone.utc).isoformat()
        BROKER.publish_job_event(
            job.job_id,
            {"kind": "job", "event_type": "dashboard_job_started", "status": job.status, "scenario_id": job.scenario_id},
        )
        try:
            with dashboard_job_context(job.job_id):
                result = RefinementRunner().run(job.scenario_id, apply_changes=job.apply_changes)
            job.result = result.as_dict()
            job.status = result.outcome
            BROKER.publish_job_event(
                job.job_id,
                {
                    "kind": "job",
                    "event_type": "dashboard_job_completed",
                    "status": job.status,
                    "result": job.result,
                },
            )
        except RefinementRunnerError as error:
            job.status = "stopped_safely"
            job.error = str(error)
            BROKER.publish_job_event(
                job.job_id,
                {"kind": "job", "event_type": "dashboard_job_failed", "status": job.status, "error": job.error},
            )
        except Exception:
            # Do not expose provider exceptions or environment details in the
            # browser. The run JSONL retains safe lifecycle evidence.
            job.status = "failed"
            job.error = "The dashboard job ended unexpectedly."
            BROKER.publish_job_event(
                job.job_id,
                {"kind": "job", "event_type": "dashboard_job_failed", "status": job.status, "error": job.error},
            )
        finally:
            job.completed_at_utc = datetime.now(timezone.utc).isoformat()


JOBS = DashboardJobStore()


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _run_detail(run_id: str) -> dict[str, Any]:
    valid_run_id = validate_run_id(run_id)
    if not valid_run_id:
        raise HTTPException(status_code=404, detail="Run not found.")
    events = read_run_events(valid_run_id)
    if not events:
        raise HTTPException(status_code=404, detail="Run not found.")
    plan = _load_json(REFINEMENT_DIR / f"{valid_run_id}.plan.json")
    applied = _load_json(REFINEMENT_DIR / f"{valid_run_id}.applied.json")
    try:
        change_diff = load_refinement_diff(valid_run_id)
    except Exception:
        change_diff = None
    return {
        "run_id": valid_run_id,
        "events": [safe_dashboard_event(event) for event in events],
        "deterministic_evaluation": _load_json(EVALUATION_DIR / f"{valid_run_id}.json"),
        "llm_evaluation": _load_json(EVALUATION_DIR / f"{valid_run_id}.llm.json"),
        "refinement_plan": plan,
        "applied_refinement": applied,
        "change_diff": change_diff,
    }


@router.get("/scenarios")
def dashboard_scenarios() -> dict[str, list[dict[str, str]]]:
    _, scenarios = load_scenarios()
    return {
        "scenarios": [
            {"id": scenario.id, "title": scenario.title, "category": scenario.category}
            for scenario in scenarios
        ]
    }


@router.post("/jobs", status_code=202)
def start_dashboard_job(request: StartDashboardJobRequest) -> dict[str, Any]:
    try:
        job = JOBS.start(request)
    except KeyError:
        raise HTTPException(status_code=422, detail="Unknown scenario.") from None
    return job.as_dict()


@router.get("/jobs/{job_id}")
def dashboard_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Dashboard job not found. Jobs are available while this server is running.")
    return job.as_dict()


@router.get("/jobs/{job_id}/events")
async def dashboard_job_events(job_id: str) -> StreamingResponse:
    if not JOBS.get(job_id):
        raise HTTPException(status_code=404, detail="Dashboard job not found.")

    async def stream() -> AsyncIterator[str]:
        subscriber_id, subscriber, history = BROKER.subscribe(job_id)
        try:
            for event in history:
                yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            while True:
                try:
                    event = await asyncio.to_thread(subscriber.get, True, 15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
        finally:
            BROKER.unsubscribe(job_id, subscriber_id)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}")
def dashboard_run(run_id: str) -> dict[str, Any]:
    """Replay a persisted run with the same shape the live dashboard consumes."""
    return _run_detail(run_id)


def mount_dashboard(app: FastAPI) -> None:
    """Attach dashboard API and static UI to the existing Ionian FastAPI app."""
    app.include_router(router)
    app.mount("/dashboard-assets", StaticFiles(directory=STATIC_DIR), name="dashboard-assets")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "dashboard.html")

