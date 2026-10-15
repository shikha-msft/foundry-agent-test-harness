"""Guardrail hooks that run before (input) and after (output) every agent call.

Built-in checks come from the spec's `guardrails.input` / `guardrails.output`:
max_chars, blocked_patterns and (output only) required_prefix. Custom hooks are
listed as "package.module:function" in `guardrails.hooks`; each is called as

    hook(stage: "input" | "output", text: str, spec: AgentSpec) -> list[str]

and returns violation messages (empty list = pass). Violations from custom
hooks follow the action of the stage they ran in.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

from .specs import AgentSpec, StageGuardrails

Hook = Callable[[str, str, AgentSpec], list[str]]

BLOCKED_INPUT_MESSAGE = "Request blocked by the input guardrail."
BLOCKED_OUTPUT_MESSAGE = "Response withheld by the output guardrail."


@dataclass
class GuardrailResult:
    stage: str
    action: str
    violations: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.violations) and self.action == "block"


@lru_cache(maxsize=None)
def load_hook(path: str) -> Hook:
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError(f"Hook {path!r} must look like 'package.module:function'")
    hook = getattr(importlib.import_module(module_name), attr)
    if not callable(hook):
        raise TypeError(f"Hook {path!r} is not callable")
    return hook


@lru_cache(maxsize=None)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p) for p in patterns)


def _builtin(stage: str, text: str, rules: StageGuardrails) -> list[str]:
    violations = []
    if rules.max_chars is not None and len(text) > rules.max_chars:
        violations.append(f"{stage} is {len(text)} chars, over the {rules.max_chars} limit")
    for pattern in _compiled(rules.blocked_patterns):
        if pattern.search(text):
            violations.append(f"{stage} matches blocked pattern {pattern.pattern!r}")
    if stage == "output" and rules.required_prefix and not text.lstrip().startswith(
        rules.required_prefix
    ):
        violations.append(f"output does not start with {rules.required_prefix!r}")
    return violations


def check(stage: str, text: str, spec: AgentSpec) -> GuardrailResult:
    rules = spec.guardrails.input if stage == "input" else spec.guardrails.output
    violations = _builtin(stage, text, rules)
    for path in spec.guardrails.hooks:
        try:
            violations.extend(load_hook(path)(stage, text, spec) or [])
        except Exception as exc:
            # A broken hook fails closed: it counts as a violation.
            violations.append(f"hook {path} failed: {exc}")
    return GuardrailResult(stage=stage, action=rules.action, violations=violations)
