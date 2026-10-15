"""Spec loading and validation, including the real agents/ specs."""

import textwrap

import pytest

from agent_harness.config import PACKAGE_ROOT
from agent_harness.specs import SpecError, load_specs


def write(tmp_path, name, body):
    (tmp_path / name).write_text(textwrap.dedent(body), encoding="utf-8")


def test_real_specs_load():
    specs = {s.key: s for s in load_specs(PACKAGE_ROOT / "agents")}
    assert set(specs) == {"randomfact", "news"}
    assert all(s.backend == "agent_framework" for s in specs.values())
    news = specs["news"]
    assert news.guardrails.output.required_prefix == "Illustrative briefing - not live news."
    assert news.guardrails.output.action == "flag"
    assert news.evaluation.kinds == ("quality", "safety")
    assert news.evaluation.dataset.exists()
    assert "Illustrative briefing" in news.instructions


def test_name_env_override(tmp_path, monkeypatch):
    write(tmp_path, "a.yaml", """
        key: a
        name: a-agent
        name_env: TEST_A_NAME
        instructions: x
    """)
    monkeypatch.setenv("TEST_A_NAME", "renamed-agent")
    assert load_specs(tmp_path)[0].name == "renamed-agent"


@pytest.mark.parametrize(
    "body, message",
    [
        ("key: a\nname: a\ninstructions: x\ncolour: red\n", "unknown field"),
        ("key: a\nname: a\n", "'instructions' is required"),
        ("key: A!\nname: a\ninstructions: x\n", "key"),
        ("key: a\nname: a\ninstructions: x\nguardrails:\n  input:\n    action: maybe\n", "action"),
        ("key: a\nname: a\ninstructions: x\nguardrails:\n  input:\n    blocked_patterns: ['(']\n", "invalid pattern"),
        ("key: a\nname: a\ninstructions: x\nevaluation:\n  kinds: [speed]\n", "evaluation.kinds"),
        ("key: a\nname: a\ninstructions: x\nevaluation:\n  fail_under: 2\n", "fail_under"),
    ],
)
def test_invalid_specs(tmp_path, body, message):
    (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(SpecError, match=message):
        load_specs(tmp_path)


def test_duplicate_keys(tmp_path):
    write(tmp_path, "a.yaml", "key: a\nname: one\ninstructions: x\n")
    write(tmp_path, "b.yaml", "key: a\nname: two\ninstructions: x\n")
    with pytest.raises(SpecError, match="Duplicate agent key"):
        load_specs(tmp_path)


def test_empty_directory(tmp_path):
    with pytest.raises(SpecError, match="No agent specs"):
        load_specs(tmp_path)


def test_validate_script_real_agents(monkeypatch, capsys):
    import validate_agents

    monkeypatch.setattr("sys.argv", ["validate_agents.py", "--dir", str(PACKAGE_ROOT / "agents")])
    assert validate_agents.main() == 0
    out = capsys.readouterr().out
    assert "ok  news" in out and "ok  randomfact" in out


def test_validate_script_rejects_eval_on_echo_backend(tmp_path, monkeypatch, capsys):
    import validate_agents

    write(tmp_path, "a.yaml", """
        key: a
        name: a-agent
        backend: echo
        instructions: x
        evaluation:
          kinds: [quality]
    """)
    monkeypatch.setattr("sys.argv", ["validate_agents.py", "--dir", str(tmp_path)])
    assert validate_agents.main() == 1
    assert "can't be evaluated" in capsys.readouterr().out
