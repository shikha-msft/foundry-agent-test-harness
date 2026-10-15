"""Example custom guardrail hooks. Reference one from a spec as

    guardrails:
      hooks:
        - agent_harness.example_hooks:no_email_addresses

A hook gets (stage, text, spec) and returns a list of violation messages.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def no_email_addresses(stage: str, text: str, spec) -> list[str]:
    """Keep email addresses out of prompts and replies."""
    if _EMAIL.search(text):
        return [f"{stage} contains an email address"]
    return []
