"""Declarative agent specs: one YAML file per agent in the agents directory.

The harness is driven entirely by these files - adding an agent means adding a
YAML file, not changing code. See agents/README.md for the schema.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import PACKAGE_ROOT, settings

EVAL_KINDS = ("quality", "safety")
ACTIONS = ("block", "flag")
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,40}$")


class SpecError(ValueError):
    """An agent spec is invalid."""


@dataclass(frozen=True)
class Limits:
    timeout_seconds: float = 60.0
    max_retries: int = 2
    # None means unlimited. Budgets are checked before each call, so one call
    # can overshoot by at most its own token count.
    session_token_budget: int | None = None
    batch_token_budget: int | None = None


@dataclass(frozen=True)
class StageGuardrails:
    action: str = "block"
    max_chars: int | None = None
    blocked_patterns: tuple[str, ...] = ()
    required_prefix: str | None = None


@dataclass(frozen=True)
class Guardrails:
    input: StageGuardrails = field(default_factory=StageGuardrails)
    output: StageGuardrails = field(default_factory=lambda: StageGuardrails(action="flag"))
    hooks: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationSpec:
    kinds: tuple[str, ...] = ()
    limit: int = 5
    fail_under: float | None = None
    dataset: Path = PACKAGE_ROOT / "data" / "eval_dataset.jsonl"


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    instructions: str
    label: str
    description: str = ""
    placeholder: str = ""
    backend: str = "foundry_prompt"
    backend_options: dict[str, Any] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    guardrails: Guardrails = field(default_factory=Guardrails)
    evaluation: EvaluationSpec = field(default_factory=EvaluationSpec)
    source: Path | None = None


def _section(raw: dict, name: str, path: Path) -> dict:
    value = raw.get(name) or {}
    if not isinstance(value, dict):
        raise SpecError(f"{path.name}: '{name}' must be a mapping")
    return value


def _known(section: dict, allowed: set[str], where: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise SpecError(f"{where}: unknown field(s) {sorted(unknown)}")


def _stage(raw: dict, default_action: str, where: str) -> StageGuardrails:
    _known(raw, {"action", "max_chars", "blocked_patterns", "required_prefix"}, where)
    action = raw.get("action", default_action)
    if action not in ACTIONS:
        raise SpecError(f"{where}.action must be one of {ACTIONS}")
    patterns = tuple(raw.get("blocked_patterns") or ())
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SpecError(f"{where}: invalid pattern {pattern!r}: {exc}") from exc
    return StageGuardrails(
        action=action,
        max_chars=raw.get("max_chars"),
        blocked_patterns=patterns,
        required_prefix=raw.get("required_prefix"),
    )


def parse_spec(raw: dict, path: Path) -> AgentSpec:
    if not isinstance(raw, dict):
        raise SpecError(f"{path.name}: top level must be a mapping")
    _known(
        raw,
        {
            "key", "name", "name_env", "label", "description", "placeholder",
            "backend", "backend_options", "instructions", "limits",
            "guardrails", "evaluation",
        },
        path.name,
    )
    for required in ("key", "name", "instructions"):
        if not raw.get(required):
            raise SpecError(f"{path.name}: '{required}' is required")

    key = raw["key"]
    if not _KEY.match(key):
        raise SpecError(f"{path.name}: key {key!r} must be lowercase letters, digits or _")

    name = os.environ.get(raw["name_env"], raw["name"]) if raw.get("name_env") else raw["name"]
    if not _NAME.match(name):
        raise SpecError(
            f"{path.name}: name {name!r} must be letters, digits or hyphens (max 41 chars)"
        )

    limits = _section(raw, "limits", path)
    _known(limits, set(Limits.__dataclass_fields__), f"{path.name}: limits")

    guardrails = _section(raw, "guardrails", path)
    _known(guardrails, {"input", "output", "hooks"}, f"{path.name}: guardrails")

    evaluation = _section(raw, "evaluation", path)
    _known(evaluation, {"kinds", "limit", "fail_under", "dataset"}, f"{path.name}: evaluation")
    kinds = tuple(evaluation.get("kinds") or ())
    bad = [k for k in kinds if k not in EVAL_KINDS]
    if bad:
        raise SpecError(f"{path.name}: evaluation.kinds {bad} not in {EVAL_KINDS}")
    fail_under = evaluation.get("fail_under")
    if fail_under is not None and not 0 <= float(fail_under) <= 1:
        raise SpecError(f"{path.name}: evaluation.fail_under must be between 0 and 1")
    dataset = evaluation.get("dataset")

    return AgentSpec(
        key=key,
        name=name,
        instructions=raw["instructions"].strip() + "\n",
        label=raw.get("label") or key,
        description=raw.get("description", ""),
        placeholder=raw.get("placeholder", ""),
        backend=raw.get("backend", "foundry_prompt"),
        backend_options=dict(raw.get("backend_options") or {}),
        limits=Limits(**limits),
        guardrails=Guardrails(
            input=_stage(guardrails.get("input") or {}, "block", f"{path.name}: guardrails.input"),
            output=_stage(guardrails.get("output") or {}, "flag", f"{path.name}: guardrails.output"),
            hooks=tuple(guardrails.get("hooks") or ()),
        ),
        evaluation=EvaluationSpec(
            kinds=kinds,
            limit=int(evaluation.get("limit", 5)),
            fail_under=float(fail_under) if fail_under is not None else None,
            dataset=(path.parent / dataset).resolve() if dataset else EvaluationSpec().dataset,
        ),
        source=path,
    )


def load_specs(directory: Path | None = None) -> list[AgentSpec]:
    directory = Path(directory or settings.agents_dir)
    files = sorted(p for p in directory.glob("*.y*ml") if p.suffix in (".yaml", ".yml"))
    if not files:
        raise SpecError(f"No agent specs (*.yaml) found in {directory}")

    specs = []
    for path in files:
        with open(path, encoding="utf-8") as handle:
            specs.append(parse_spec(yaml.safe_load(handle), path))

    for attr in ("key", "name"):
        seen: dict[str, Path] = {}
        for spec in specs:
            value = getattr(spec, attr)
            if value in seen:
                raise SpecError(
                    f"Duplicate agent {attr} {value!r} in {seen[value].name} and {spec.source.name}"
                )
            seen[value] = spec.source
    return specs
