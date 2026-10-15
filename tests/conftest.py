"""Offline test setup: echo-backend agents, no Azure calls.

Runs before any agent_harness import, so the registry loads the fixture specs.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ["HARNESS_AGENTS_DIR"] = str(ROOT / "tests" / "fixtures" / "agents")
# Settings requires these; the echo backend never uses them.
os.environ.setdefault("AZURE_AI_PROJECT_ENDPOINT", "https://example.invalid/api/projects/test")
os.environ.setdefault("AZURE_AI_MODEL_DEPLOYMENT_NAME", "test-model")
