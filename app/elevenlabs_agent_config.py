"""Read the versioned ElevenLabs configuration used by refinement planning.

This module intentionally exposes a read-only client.  A separate applier will
later own the guarded PATCH operation after the plan format and verification
workflow have been exercised on a dedicated branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from elevenlabs.client import ElevenLabs
from elevenlabs.core.api_error import ApiError

from app.config import ELEVENLABS_AGENT_ID, ELEVENLABS_API_KEY


class ElevenLabsAgentConfigError(RuntimeError):
    """Raised when the configured agent branch cannot be read safely."""


@dataclass(frozen=True)
class AgentPromptSnapshot:
    """The only configuration data the Refiner needs at planning time."""

    agent_id: str
    branch_id: str
    system_prompt: str
    version_id: str | None


def _as_dict(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        payload = response.model_dump(mode="json", exclude_none=False)
    elif isinstance(response, dict):
        payload = response
    else:
        raise ElevenLabsAgentConfigError("ElevenLabs returned an unsupported agent configuration response.")
    if not isinstance(payload, dict):
        raise ElevenLabsAgentConfigError("ElevenLabs returned an invalid agent configuration response.")
    return payload


def _provider_error_summary(error: ApiError) -> str:
    """Return enough diagnostics to fix configuration, without exposing headers."""
    status = f"HTTP {error.status_code}" if error.status_code is not None else "transport error"
    body = error.body
    if isinstance(body, dict):
        detail = body.get("detail", body)
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("code") or detail.get("type")
            if isinstance(message, str):
                return f"{status}: {message}"
        if isinstance(detail, str):
            return f"{status}: {detail}"
    return status


class ElevenLabsAgentConfigClient:
    """Retrieve Erling's prompt from one explicit non-main branch."""

    def __init__(
        self,
        *,
        agent_id: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.agent_id = agent_id or ELEVENLABS_AGENT_ID
        if not self.agent_id:
            raise ElevenLabsAgentConfigError("ELEVENLABS_AGENT_ID is not configured in keys.env.")
        if client is None:
            key = api_key if api_key is not None else ELEVENLABS_API_KEY
            if not key:
                raise ElevenLabsAgentConfigError("ELEVENLABS_API_KEY is not configured in keys.env.")
            client = ElevenLabs(api_key=key)
        self._client = client

    def get_system_prompt(self, *, branch_id: str) -> AgentPromptSnapshot:
        """Read the prompt attached to exactly the requested agent branch."""
        if not branch_id.strip():
            raise ValueError("A non-empty dedicated ElevenLabs refinement branch_id is required.")
        try:
            response = self._client.conversational_ai.agents.get(self.agent_id, branch_id=branch_id)
            payload = _as_dict(response)
            prompt = payload["conversation_config"]["agent"]["prompt"]["prompt"]
        except (KeyError, TypeError, ValueError) as error:
            raise ElevenLabsAgentConfigError("The selected branch does not expose a text system prompt.") from error
        except ApiError as error:
            raise ElevenLabsAgentConfigError(
                f"Unable to retrieve the ElevenLabs agent branch configuration ({_provider_error_summary(error)})."
            ) from error
        except Exception as error:
            raise ElevenLabsAgentConfigError(
                f"Unable to retrieve the ElevenLabs agent branch configuration ({type(error).__name__})."
            ) from error
        if not isinstance(prompt, str) or not prompt.strip():
            raise ElevenLabsAgentConfigError("The selected ElevenLabs branch has no non-empty system prompt.")
        returned_branch_id = payload.get("branch_id")
        if returned_branch_id not in {None, branch_id}:
            raise ElevenLabsAgentConfigError("ElevenLabs returned a configuration for a different branch.")
        returned_agent_id = payload.get("agent_id")
        if returned_agent_id not in {None, self.agent_id}:
            raise ElevenLabsAgentConfigError("ElevenLabs returned a configuration for a different agent.")
        version_id = payload.get("version_id")
        return AgentPromptSnapshot(
            agent_id=self.agent_id,
            branch_id=branch_id,
            system_prompt=prompt,
            version_id=version_id if isinstance(version_id, str) else None,
        )
