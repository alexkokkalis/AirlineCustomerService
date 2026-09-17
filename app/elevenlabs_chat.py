"""Small application-facing client for an ElevenLabs Chat Mode agent.

The dashboard owns Erling's prompt, tools, and versioning.  This module only
opens a text conversation, sends customer messages, and returns Erling's text
replies.  Keeping that transport boundary separate lets the later simulator,
evaluation runner, and UI use the same integration.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep

import certifi
import httpx

# Python installations on macOS can lack a usable system certificate store.
# Pin WebSocket/API TLS validation to certifi's bundled Mozilla CA set so this
# integration is reproducible inside the project's virtual environment.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation, ConversationInitiationData

from app.config import ELEVENLABS_AGENT_ID, ELEVENLABS_API_KEY
from app.run_logging import append_run_event, validate_run_id


ROOT = Path(__file__).resolve().parents[1]
AGENT_EVENT_LOG_PATH = ROOT / "logs" / "agent_events.jsonl"


class ElevenLabsChatError(RuntimeError):
    """Raised when a Chat Mode session cannot be configured or complete a turn."""


@dataclass(frozen=True)
class ChatReply:
    """One completed agent reply, with metadata useful to a test runner."""

    text: str
    conversation_id: str | None
    messages: tuple[str, ...] = ()


class _BranchConversation(Conversation):
    """Conversation transport whose signed URL is pinned to an agent branch."""

    def __init__(self, *args: object, branch_id: str, api_key: str, **kwargs: object) -> None:
        self._branch_id = branch_id
        self._api_key = api_key
        super().__init__(*args, **kwargs)

    def _get_signed_url(self) -> str:
        """Use the documented signed-URL endpoint because this SDK version lacks branch_id."""
        response = httpx.get(
            "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
            params={"agent_id": self.agent_id, "branch_id": self._branch_id},
            headers={"xi-api-key": self._api_key},
            timeout=10,
            verify=certifi.where(),
        )
        response.raise_for_status()
        signed_url = response.json().get("signed_url")
        if not isinstance(signed_url, str) or not signed_url:
            raise ElevenLabsChatError("ElevenLabs did not return a usable signed URL for the requested branch.")
        return signed_url


def _write_agent_event(event: dict) -> None:
    """Log operational metadata only; transcripts are stored by the test runner."""
    try:
        AGENT_EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AGENT_EVENT_LOG_PATH.open("a") as log_file:
            log_file.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        # Observability must never make an active customer session fail.
        pass


class ElevenLabsChatSession:
    """A synchronous, multi-turn text session with the configured ElevenLabs agent.

    ``send_message`` blocks only until a complete agent text response arrives.
    The ElevenLabs SDK performs the WebSocket work in the background; a small
    condition variable bridges its callback-based API to the rest of this
    synchronous Python application.
    """

    def __init__(
        self,
        *,
        agent_id: str | None = None,
        api_key: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        branch_id: str | None = None,
        force_text_only: bool = False,
    ) -> None:
        self.agent_id = agent_id or ELEVENLABS_AGENT_ID
        self.api_key = api_key if api_key is not None else ELEVENLABS_API_KEY
        self.user_id = user_id or f"ionian-local-{uuid.uuid4()}"
        self.run_id = validate_run_id(run_id)
        self.branch_id = branch_id.strip() if isinstance(branch_id, str) and branch_id.strip() else None
        self.force_text_only = force_text_only
        self._condition = threading.Condition()
        self._responses: list[str] = []
        self.initial_greeting: ChatReply | None = None
        self._conversation: Conversation | None = None
        self._closed = False

    @property
    def conversation_id(self) -> str | None:
        """Return the provider's ID after the session has connected."""
        # The current SDK exposes this value after session establishment as an
        # internal attribute rather than a public property. Keeping access in
        # this one adapter prevents that SDK detail leaking into callers.
        return getattr(self._conversation, "_conversation_id", None) if self._conversation else None

    def start(
        self,
        *,
        connection_timeout_seconds: float = 10,
        greeting_timeout_seconds: float = 5,
    ) -> None:
        """Open the text conversation once; safe to call repeatedly."""
        if self._conversation:
            return
        if not self.agent_id:
            raise ElevenLabsChatError("ELEVENLABS_AGENT_ID is not configured. Add it to keys.env.")
        if not self.api_key:
            raise ElevenLabsChatError("ELEVENLABS_API_KEY is not configured. Add it to keys.env.")

        # The dashboard is already configured as Text only. The optional runtime
        # override is useful for future non-production tests, but requires that
        # the equivalent security override is enabled in ElevenLabs.
        config = None
        if self.force_text_only or self.run_id:
            config = ConversationInitiationData(
                conversation_config_override={"conversation": {"text_only": True}} if self.force_text_only else None,
                dynamic_variables={"run_id": self.run_id} if self.run_id else None,
            )

        conversation_args = (
            ElevenLabs(api_key=self.api_key),
            self.agent_id,
        )
        conversation_kwargs = {
            "user_id": self.user_id,
            "requires_auth": True,
            "config": config,
            "callback_agent_response": self._on_agent_response,
            "callback_end_session": self._on_end_session,
        }
        if self.branch_id:
            self._conversation = _BranchConversation(
                *conversation_args,
                branch_id=self.branch_id,
                api_key=self.api_key,
                **conversation_kwargs,
            )
        else:
            self._conversation = Conversation(*conversation_args, **conversation_kwargs)
        append_run_event(
            self.run_id,
            "elevenlabs_session_starting",
            agent_id=self.agent_id,
            branch_id=self.branch_id,
            user_id=self.user_id,
        )
        try:
            self._conversation.start_session()
        except Exception as error:
            # Provider authentication and network errors originate inside the
            # SDK before its background WebSocket thread exists. Present a
            # stable application-level error to callers while retaining the
            # original exception as diagnostic context.
            self._conversation = None
            raise ElevenLabsChatError(f"Unable to start ElevenLabs Chat Mode: {error}") from error
        self._wait_for_connection(connection_timeout_seconds)
        _write_agent_event(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "event_type": "elevenlabs_chat_started",
                "agent_id": self.agent_id,
                "conversation_id": self.conversation_id,
            }
        )
        append_run_event(
            self.run_id,
            "elevenlabs_session_connected",
            agent_id=self.agent_id,
            branch_id=self.branch_id,
            conversation_id=self.conversation_id,
        )
        # Erling is configured with an automatic greeting. It is an agent turn,
        # not the answer to the first user message; consume it before callers
        # are allowed to send a customer turn. Without this separation, a fast
        # greeting can be mistaken for the next reply and the session closed
        # while the real request is still running.
        greeting_messages = self._collect_responses_after(
            response_count=0,
            timeout_seconds=greeting_timeout_seconds,
            quiet_period_seconds=0.25,
            required=False,
        )
        if greeting_messages:
            self.initial_greeting = ChatReply(
                text=greeting_messages[-1],
                conversation_id=self.conversation_id,
                messages=tuple(greeting_messages),
            )

    def send_message(self, customer_message: str, *, timeout_seconds: float = 30) -> ChatReply:
        """Send one customer text message and wait for Erling's complete reply."""
        message = customer_message.strip()
        if not message:
            raise ValueError("customer_message must not be empty.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")
        self.start()
        if self._closed or not self._conversation:
            raise ElevenLabsChatError("The Chat Mode session is closed.")

        with self._condition:
            response_count = len(self._responses)
            append_run_event(self.run_id, "message", role="customer", text=message)
            self._conversation.send_user_message(message)
        messages = self._collect_responses_after(
            response_count=response_count,
            timeout_seconds=timeout_seconds,
            quiet_period_seconds=2,
            required=True,
        )
        response = messages[-1]

        _write_agent_event(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "event_type": "elevenlabs_chat_turn_completed",
                "agent_id": self.agent_id,
                "conversation_id": self.conversation_id,
                "response_characters": len(response),
            }
        )
        return ChatReply(
            text=response,
            conversation_id=self.conversation_id,
            messages=tuple(messages),
        )

    def close(self) -> str | None:
        """End the provider session and return its conversation ID when available."""
        if self._conversation and not self._closed:
            self._conversation.end_session()
        self._closed = True
        with self._condition:
            self._condition.notify_all()
        append_run_event(
            self.run_id,
            "elevenlabs_session_closed",
            conversation_id=self.conversation_id,
        )
        return self.conversation_id

    def _on_agent_response(self, response: str) -> None:
        if not response:
            return
        with self._condition:
            self._responses.append(response)
            self._condition.notify_all()
        append_run_event(
            self.run_id,
            "message",
            role="agent",
            text=response,
            conversation_id=self.conversation_id,
        )

    def _wait_for_connection(self, timeout_seconds: float) -> None:
        """Avoid racing ``send_user_message`` against the SDK's socket thread."""
        if timeout_seconds <= 0:
            raise ValueError("connection_timeout_seconds must be greater than zero.")
        deadline = monotonic() + timeout_seconds
        while self._conversation and getattr(self._conversation, "_ws", None) is None:
            if monotonic() >= deadline:
                self.close()
                raise ElevenLabsChatError(
                    f"ElevenLabs Chat Mode did not connect within {timeout_seconds} seconds."
                )
            sleep(0.02)

    def _collect_responses_after(
        self,
        *,
        response_count: int,
        timeout_seconds: float,
        quiet_period_seconds: float,
        required: bool,
    ) -> list[str]:
        """Collect replies until Erling is quiet after a completed text event.

        ElevenLabs can send a short acknowledgement before a webhook call and a
        second, substantive reply after the tool result. Its Python SDK does not
        expose an explicit "turn complete" event, so a short quiet period gives
        the agent's tool workflow time to finish without ending the session
        after the first acknowledgement.
        """
        deadline = monotonic() + timeout_seconds
        with self._condition:
            while len(self._responses) == response_count and not self._closed:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    if required:
                        raise ElevenLabsChatError(
                            f"ElevenLabs did not return a text response within {timeout_seconds} seconds."
                        )
                    return []
                self._condition.wait(timeout=remaining)

            if len(self._responses) == response_count:
                if required:
                    raise ElevenLabsChatError("The ElevenLabs Chat Mode session closed before responding.")
                return []

            last_response_count = len(self._responses)
            while not self._closed:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=min(quiet_period_seconds, remaining))
                if len(self._responses) != last_response_count:
                    last_response_count = len(self._responses)
                    continue
                break

            return self._responses[response_count:]

    def _on_end_session(self) -> None:
        self._closed = True
        with self._condition:
            self._condition.notify_all()

    def __enter__(self) -> "ElevenLabsChatSession":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
