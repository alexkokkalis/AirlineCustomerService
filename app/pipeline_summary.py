"""Create one portable, structured evidence summary for a refinement pipeline."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
PIPELINE_SUMMARY_DIR = ROOT / "logs" / "pipeline_summaries"


def pipeline_summary_path(pipeline_id: str) -> Path:
    """Return the durable location for one pipeline-level summary."""
    if not pipeline_id.startswith("pipeline_") or not pipeline_id.replace("_", "").isalnum():
        raise ValueError("pipeline_id must start with 'pipeline_' and contain only letters, numbers, or underscores.")
    return PIPELINE_SUMMARY_DIR / f"{pipeline_id}.json"


def _iteration_summary(iteration: dict[str, Any]) -> dict[str, Any]:
    simulation = iteration["simulation"]
    deterministic = simulation.get("evaluation") or {}
    llm = simulation.get("llm_evaluation") or {}
    plan = iteration.get("plan") or {}
    applied = iteration.get("applied") or {}
    failed_checks = [
        {"id": check.get("id"), "message": check.get("message"), "evidence": check.get("evidence", [])}
        for check in deterministic.get("checks", [])
        if check.get("status") == "fail"
    ]
    failures = [
        {
            "criterion": failure.get("criterion"),
            "root_cause": failure.get("root_cause"),
            "explanation": failure.get("explanation"),
            "recommended_change": failure.get("recommended_change"),
        }
        for failure in llm.get("failures", [])
    ]
    return {
        "iteration": iteration["iteration"],
        "run_id": simulation["run_id"],
        "conversation_id": simulation.get("conversation_id"),
        "outcome": simulation.get("outcome"),
        "deterministic": {
            "overall_status": deterministic.get("overall_status"),
            "failed_checks": failed_checks,
        },
        "llm_evaluation": {
            "overall_score": llm.get("overall_score"),
            "criterion_scores": {
                name: details.get("score") for name, details in (llm.get("criteria") or {}).items()
            },
            "decision": (llm.get("decision") or {}).get("decision"),
            "root_causes": (llm.get("decision") or {}).get("root_causes", []),
            "failures": failures,
        },
        "refinement": {
            "status": plan.get("status"),
            "target": plan.get("target"),
            "target_file": plan.get("target_file"),
            "operation": plan.get("operation"),
            "rationale": plan.get("rationale"),
            "expected_effect": plan.get("expected_effect"),
            "verification_scenario_id": plan.get("verification_scenario_id"),
            "apply_status": applied.get("status"),
            "change_diff": applied.get("change_diff"),
        },
    }


def _performance_improvement(iterations: list[dict[str, Any]]) -> dict[str, Any]:
    first, final = iterations[0], iterations[-1]
    first_score = first["llm_evaluation"]["overall_score"]
    final_score = final["llm_evaluation"]["overall_score"]
    score_delta = final_score - first_score if isinstance(first_score, int) and isinstance(final_score, int) else None
    first_roots = set(first["llm_evaluation"]["root_causes"])
    final_roots = set(final["llm_evaluation"]["root_causes"])
    return {
        "first_run_id": first["run_id"],
        "final_run_id": final["run_id"],
        "overall_score_before": first_score,
        "overall_score_after": final_score,
        "overall_score_delta": score_delta,
        "deterministic_status_before": first["deterministic"]["overall_status"],
        "deterministic_status_after": final["deterministic"]["overall_status"],
        "decision_before": first["llm_evaluation"]["decision"],
        "decision_after": final["llm_evaluation"]["decision"],
        "root_causes_resolved": sorted(first_roots - final_roots),
    }


def write_pipeline_summary(
    *,
    pipeline_id: str,
    scenario_id: str,
    outcome: str,
    iterations: Iterable[dict[str, Any]],
    guardrails: dict[str, int],
    applied_changes: int,
    dry_run: bool,
    stopped_reason: str | None = None,
) -> Path:
    """Persist exactly one compact summary of a full refinement pipeline.

    The per-run JSONL files remain the complete audit trail. This artifact is
    the assessment-friendly index: it links each iteration's scores, root
    cause, selected change, and measurable first-to-final outcome without
    duplicating raw customer messages or credentials.
    """
    summarized_iterations = [_iteration_summary(iteration) for iteration in iterations]
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pipeline_id": pipeline_id,
        "scenario_id": scenario_id,
        "outcome": outcome,
        "dry_run": dry_run,
        "guardrails": guardrails,
        "applied_changes": applied_changes,
        "iterations": summarized_iterations,
        "performance_improvement": _performance_improvement(summarized_iterations) if summarized_iterations else None,
        "stopped_reason": stopped_reason,
    }
    path = pipeline_summary_path(pipeline_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path
