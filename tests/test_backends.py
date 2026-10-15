"""Plugin backends registered through HARNESS_PLUGINS."""

import textwrap

import pytest

from agent_harness import backends
from agent_harness.specs import load_specs


def test_plugin_registers_backend(tmp_path, monkeypatch):
    (tmp_path / "my_plugin.py").write_text(
        textwrap.dedent(
            """
            from agent_harness.backends import BackendResponse, EchoBackend, register_backend

            class Shouty(EchoBackend):
                def invoke(self, spec, name, prompt, conversation_id):
                    return BackendResponse(prompt.upper(), 1, 1, 2)

            register_backend("shouty", Shouty())
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    backends.load_plugins("my_plugin")

    (tmp_path / "a.yaml").write_text("key: a\nname: a\nbackend: shouty\ninstructions: x\n")
    spec = load_specs(tmp_path)[0]
    assert backends.get_backend(spec.backend).invoke(spec, "a", "hi", None).output_text == "HI"


def test_unknown_backend_message():
    with pytest.raises(KeyError, match="registered"):
        backends.get_backend("nope")
