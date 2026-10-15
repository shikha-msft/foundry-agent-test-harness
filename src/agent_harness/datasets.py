"""JSONL prompt datasets: one {"agent": "<key>", "query": "..."} object per line.

Rows without an `agent` field apply to every agent.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_prompts(
    path: str | Path, agent_key: str | None = None, limit: int | None = None
) -> list[str]:
    prompts: list[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if agent_key and record.get("agent") not in (None, agent_key):
                continue
            value = record.get("query") or record.get("prompt")
            if value:
                prompts.append(value)
    return prompts[:limit] if limit else prompts
