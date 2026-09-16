"""Local application configuration loaded from the gitignored keys.env file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "keys.env")

# When set, this token is required on all agent-callable API routes.
IONIAN_TOOL_TOKEN = os.getenv("IONIAN_TOOL_TOKEN")

# ElevenLabs agent used by the application-facing Chat Mode client.  These are
# intentionally loaded from the gitignored keys.env file, never source code.
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID")

# OpenAI is used only by the assessment customer simulator.  This is separate
# from Erling, which continues to run in ElevenLabs Chat Mode.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_SIMULATOR_MODEL = os.getenv("OPENAI_SIMULATOR_MODEL", "gpt-5-mini")
