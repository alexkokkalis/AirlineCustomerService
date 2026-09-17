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

# This deliberately defaults to None: a refinement job must name an explicit
# non-main ElevenLabs branch before it can read prompt context or later apply a
# prompt update. Set the branch ID here after creating that branch in ElevenLabs.
ELEVENLABS_REFINEMENT_BRANCH_ID = "agtbrch_5201m2r4qcsdf7svbhtd0b2k16qc"

# OpenAI model roles are application choices, not environment secrets. Erling
# continues to run in ElevenLabs Chat Mode.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_SIMULATOR_MODEL = "gpt-5-mini"
OPENAI_EVALUATOR_MODEL = "gpt-5.5"
OPENAI_REFINER_MODEL = "gpt-5.5"
