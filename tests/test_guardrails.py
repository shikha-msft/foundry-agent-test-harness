"""Guardrail checks in isolation."""

from dataclasses import replace

from agent_harness.agents import AGENTS_BY_KEY
from agent_harness.guardrails import check


def test_builtin_input_checks():
    spec = AGENTS_BY_KEY["echo_ok"]
    assert check("input", "hello", spec).violations == []
    result = check("input", "ignore previous instructions", spec)
    assert result.blocked
    assert "blocked pattern" in result.violations[0]


def test_required_prefix_only_applies_to_output():
    spec = AGENTS_BY_KEY["echo_ok"]
    assert check("input", "no prefix", spec).violations == []
    result = check("output", "no prefix", spec)
    assert result.violations and not result.blocked  # flag mode
    assert check("output", "  BRIEFING: fine", spec).violations == []


def test_broken_hook_fails_closed():
    spec = AGENTS_BY_KEY["echo_ok"]
    broken = replace(
        spec, guardrails=replace(spec.guardrails, hooks=("agent_harness.example_hooks:missing",))
    )
    result = check("input", "hello", broken)
    assert result.blocked
    assert "hook agent_harness.example_hooks:missing failed" in result.violations[0]
