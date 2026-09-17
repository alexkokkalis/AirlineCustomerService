"""Deterministic, guarded application of one validated refinement plan.

The Refiner never has mutation authority. This module owns the small permitted
surface: one exact source replacement on the dedicated Git branch, or one
exact prompt replacement on the dedicated ElevenLabs branch.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import certifi
import httpx

from app.config import ELEVENLABS_AGENT_ID, ELEVENLABS_API_KEY, ELEVENLABS_REFINEMENT_BRANCH_ID
from app.elevenlabs_agent_config import ElevenLabsAgentConfigClient, ElevenLabsAgentConfigError
from app.refiner import ALLOWED_CODE_FILES, REFINEMENT_DIR, RefinementPlan
from app.run_logging import append_run_event


ROOT = Path(__file__).resolve().parents[1]
REFINEMENT_GIT_BRANCH = "autonomous-refinement"
ELEVENLABS_AGENT_URL = "https://api.elevenlabs.io/v1/convai/agents"


class RefinementApplyError(RuntimeError):
    """Raised when a plan cannot be applied without violating a safeguard."""


@dataclass(frozen=True)
class AppliedRefinement:
    run_id: str
    status: Literal["no_change", "dry_run_validated", "applied"]
    target: str
    target_file: str
    branch_id: str | None
    verification_scenario_id: str
    apply_path: str | None
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _current_git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RefinementApplyError("Unable to determine the current Git branch for a code refinement.") from error
    return result.stdout.strip()


def _require_refinement_git_branch() -> None:
    current_branch = _current_git_branch()
    if current_branch != REFINEMENT_GIT_BRANCH:
        raise RefinementApplyError(
            f"Code refinement is permitted only on Git branch {REFINEMENT_GIT_BRANCH!r}; current branch is {current_branch!r}."
        )


def _plan_apply_path(run_id: str) -> Path:
    return REFINEMENT_DIR / f"{run_id}.applied.json"


def load_refinement_plan(run_id: str) -> RefinementPlan:
    """Load a durable plan produced by the structured Refiner call."""
    path = REFINEMENT_DIR / f"{run_id}.plan.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RefinementApplyError("The requested refinement plan is unavailable or invalid.") from error
    try:
        return RefinementPlan(**payload)
    except (TypeError, ValueError) as error:
        raise RefinementApplyError("The requested refinement plan has an unsupported structure.") from error


class RefinementApplier:
    """Validate and optionally apply exactly one plan; default is non-mutating."""

    def __init__(
        self,
        *,
        agent_config_client: ElevenLabsAgentConfigClient | Any | None = None,
        refinement_branch_id: str | None = None,
        http_client: httpx.Client | Any | None = None,
    ) -> None:
        self.refinement_branch_id = refinement_branch_id or ELEVENLABS_REFINEMENT_BRANCH_ID
        self._agent_config_client = agent_config_client
        self._http_client = http_client

    def apply(self, plan: RefinementPlan, *, apply: bool = False) -> AppliedRefinement:
        """Validate a plan, mutating only when ``apply=True`` and all checks pass."""
        if plan.status == "no_change":
            if plan.target != "none" or plan.operation != "none":
                raise RefinementApplyError("A no-change plan must not contain an actionable target.")
            result = AppliedRefinement(
                run_id=plan.run_id,
                status="no_change",
                target=plan.target,
                target_file=plan.target_file,
                branch_id=self.refinement_branch_id,
                verification_scenario_id=plan.verification_scenario_id,
                apply_path=None,
                dry_run=not apply,
            )
            append_run_event(plan.run_id, "refinement_apply_skipped", reason="no_change")
            return result
        if plan.status != "refinement_needed" or plan.operation != "replace":
            raise RefinementApplyError("Only one validated replace refinement can be applied.")

        append_run_event(
            plan.run_id,
            "refinement_apply_started",
            target=plan.target,
            target_file=plan.target_file,
            expected_text_digest=_digest(plan.expected_current_text),
            replacement_text_digest=_digest(plan.replacement_text),
            dry_run=not apply,
        )
        if plan.target == "code":
            result = self._apply_code(plan, apply=apply)
        elif plan.target == "erling_prompt":
            result = self._apply_prompt(plan, apply=apply)
        else:
            raise RefinementApplyError("The plan target is not actionable.")
        self._write_apply_record(result)
        append_run_event(
            plan.run_id,
            "refinement_apply_completed",
            status=result.status,
            target=result.target,
            target_file=result.target_file,
            branch_id=result.branch_id,
            apply_path=result.apply_path,
            dry_run=result.dry_run,
        )
        return result

    def _apply_code(self, plan: RefinementPlan, *, apply: bool) -> AppliedRefinement:
        if plan.target_file not in ALLOWED_CODE_FILES:
            raise RefinementApplyError("The plan targets a source file outside the code allowlist.")
        target_path = ROOT / plan.target_file
        try:
            current = target_path.read_text()
        except OSError as error:
            raise RefinementApplyError("The planned source file cannot be read.") from error
        if current.count(plan.expected_current_text) != 1:
            raise RefinementApplyError("The planned source text must occur exactly once before code refinement.")
        if apply:
            _require_refinement_git_branch()
            replacement = current.replace(plan.expected_current_text, plan.replacement_text, 1)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target_path.parent, delete=False) as temporary:
                temporary.write(replacement)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target_path)
        return AppliedRefinement(
            run_id=plan.run_id,
            status="applied" if apply else "dry_run_validated",
            target=plan.target,
            target_file=plan.target_file,
            branch_id=REFINEMENT_GIT_BRANCH if apply else None,
            verification_scenario_id=plan.verification_scenario_id,
            apply_path=str(_plan_apply_path(plan.run_id)) if apply else None,
            dry_run=not apply,
        )

    def _apply_prompt(self, plan: RefinementPlan, *, apply: bool) -> AppliedRefinement:
        if plan.target_file != "elevenlabs_system_prompt" or not self.refinement_branch_id:
            raise RefinementApplyError("Prompt refinement requires the configured dedicated ElevenLabs branch.")
        try:
            config_client = self._agent_config_client or ElevenLabsAgentConfigClient()
            snapshot = config_client.get_system_prompt(branch_id=self.refinement_branch_id)
        except (ElevenLabsAgentConfigError, ValueError) as error:
            raise RefinementApplyError("Unable to re-read the ElevenLabs refinement-branch prompt.") from error
        if snapshot.system_prompt.count(plan.expected_current_text) != 1:
            raise RefinementApplyError("The planned prompt text must occur exactly once immediately before update.")
        if apply:
            updated_prompt = snapshot.system_prompt.replace(plan.expected_current_text, plan.replacement_text, 1)
            self._update_prompt(branch_id=snapshot.branch_id, prompt=updated_prompt, version_description=plan.rationale)
            try:
                verified = config_client.get_system_prompt(branch_id=snapshot.branch_id)
            except (ElevenLabsAgentConfigError, ValueError) as error:
                raise RefinementApplyError("Prompt update was sent but could not be re-read for verification.") from error
            if verified.system_prompt != updated_prompt:
                raise RefinementApplyError("ElevenLabs did not persist the exact verified prompt update.")
        return AppliedRefinement(
            run_id=plan.run_id,
            status="applied" if apply else "dry_run_validated",
            target=plan.target,
            target_file=plan.target_file,
            branch_id=snapshot.branch_id,
            verification_scenario_id=plan.verification_scenario_id,
            apply_path=str(_plan_apply_path(plan.run_id)) if apply else None,
            dry_run=not apply,
        )

    def _update_prompt(self, *, branch_id: str, prompt: str, version_description: str) -> None:
        if not ELEVENLABS_AGENT_ID or not ELEVENLABS_API_KEY:
            raise RefinementApplyError("ElevenLabs agent credentials are not configured.")
        if not branch_id.startswith("agtbrch_"):
            raise RefinementApplyError("Prompt refinement requires an explicit non-main ElevenLabs branch ID.")
        payload = {
            "conversation_config": {"agent": {"prompt": {"prompt": prompt}}},
            "version_description": f"Autonomous refinement: {version_description[:180]}",
        }
        owns_client = self._http_client is None
        client = self._http_client or httpx.Client(timeout=20, verify=certifi.where())
        try:
            response = client.patch(
                f"{ELEVENLABS_AGENT_URL}/{ELEVENLABS_AGENT_ID}",
                params={"branch_id": branch_id},
                headers={"xi-api-key": ELEVENLABS_API_KEY},
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            status = getattr(error.response, "status_code", None) if hasattr(error, "response") else None
            raise RefinementApplyError(f"ElevenLabs prompt update failed{f' (HTTP {status})' if status else ''}.") from error
        finally:
            if owns_client:
                client.close()

    @staticmethod
    def _write_apply_record(result: AppliedRefinement) -> None:
        if result.status != "applied" or not result.apply_path:
            return
        path = Path(result.apply_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result.as_dict(), indent=2) + "\n")
