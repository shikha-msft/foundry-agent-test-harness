"""Pluggable agent backends.

The harness talks to agents only through the AgentBackend protocol, so a spec's
`backend:` field decides where it runs. Built in:

  foundry_prompt   Foundry prompt agent (versioned, Entra Agent ID, server-side evals)
  echo             Offline stand-in that needs no Azure - for tests and dry runs
  agent_framework  Microsoft Agent Framework agent run in this same process,
                   calling a Foundry model deployment directly - Foundry never
                   sees an agent resource, only inference calls

Register more with register_backend("name", instance). To do that without
touching this package, put the call in your own module and list it in
HARNESS_PLUGINS.
"""

from __future__ import annotations

import importlib
import os
import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .config import settings  # also loads .env before HARNESS_PLUGINS is read, below

if TYPE_CHECKING:
    from .specs import AgentSpec


@dataclass
class BackendResponse:
    output_text: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ProvisionedVersion:
    name: str
    version: str
    id: str | None = None


class AgentBackend(Protocol):
    #: Foundry's server-side evaluations can target this backend's agents.
    supports_foundry_evaluation: bool
    #: Agents on this backend have an Entra Agent ID.
    supports_agent_identity: bool
    #: Can be evaluated with the local Azure AI Evaluation SDK instead (no
    #: Foundry agent resource needed). False for backends with nothing
    #: meaningful to evaluate, like the offline echo stub.
    supports_local_evaluation: bool

    def ensure_ready(self) -> None:
        """Set up clients and telemetry before the first span."""

    def provision(self, spec: "AgentSpec", name: str, model: str) -> ProvisionedVersion: ...

    def latest_version(self, name: str) -> str | None: ...

    def start_conversation(self) -> str | None: ...

    def invoke(
        self,
        spec: "AgentSpec",
        name: str,
        prompt: str,
        conversation_id: str | None,
        model: str,
    ) -> BackendResponse: ...

    def span_attributes(self, name: str) -> dict[str, str]:
        """Extra attributes for the invoke span (for example identity)."""


class FoundryPromptBackend:
    supports_foundry_evaluation = True
    supports_agent_identity = True
    supports_local_evaluation = False  # never reached; Foundry evaluation takes priority

    def ensure_ready(self) -> None:
        from .client import get_clients

        get_clients()

    def provision(self, spec, name, model):
        from azure.ai.projects.models import PromptAgentDefinition

        from .client import get_clients

        agent = get_clients().project.agents.create_version(
            agent_name=name,
            definition=PromptAgentDefinition(model=model, instructions=spec.instructions),
        )
        return ProvisionedVersion(name=agent.name, version=agent.version, id=agent.id)

    def latest_version(self, name):
        from azure.core.exceptions import ResourceNotFoundError

        from .client import get_clients

        try:
            versions = list(get_clients().project.agents.list_versions(agent_name=name))
        except ResourceNotFoundError:
            return None
        if not versions:
            return None
        return max(versions, key=lambda v: int(v.version)).version

    def start_conversation(self):
        from .client import get_clients

        return get_clients().openai.conversations.create().id

    def invoke(self, spec, name, prompt, conversation_id, model):
        from .client import get_clients

        # The SDK retries 408/429/5xx with backoff; the spec sets how hard.
        openai = get_clients().openai.with_options(
            timeout=spec.limits.timeout_seconds,
            max_retries=spec.limits.max_retries,
        )
        request: dict[str, Any] = {
            "extra_body": {"agent_reference": {"name": name, "type": "agent_reference"}},
            "input": prompt,
        }
        # The API rejects a null 'conversation', so only send it when set.
        if conversation_id:
            request["conversation"] = conversation_id

        response = openai.responses.create(**request)
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        return BackendResponse(
            output_text=response.output_text or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=getattr(usage, "total_tokens", None) or input_tokens + output_tokens,
        )

    def span_attributes(self, name):
        from .identity import cached_identity

        info = cached_identity(name)
        return info.span_attributes() if info else {}


class EchoBackend:
    """Deterministic and offline. `backend_options.reply` is a format string
    with {prompt} and {name}; tokens are counted as whitespace-separated words."""

    supports_foundry_evaluation = False
    supports_agent_identity = False
    supports_local_evaluation = False  # a canned-response stub has nothing worth evaluating

    def __init__(self) -> None:
        self._versions: dict[str, int] = {}

    def ensure_ready(self) -> None:
        return None

    def provision(self, spec, name, model):
        self._versions[name] = self._versions.get(name, 0) + 1
        return ProvisionedVersion(name=name, version=str(self._versions[name]))

    def latest_version(self, name):
        # Echo agents exist implicitly, so they are always "provisioned".
        return str(self._versions.get(name, 1))

    def start_conversation(self):
        return f"echo-{uuid.uuid4()}"

    def invoke(self, spec, name, prompt, conversation_id, model):
        template = spec.backend_options.get("reply", "{prompt}")
        text = template.format(prompt=prompt, name=name)
        input_tokens, output_tokens = len(prompt.split()), len(text.split())
        return BackendResponse(text, input_tokens, output_tokens, input_tokens + output_tokens)

    def span_attributes(self, name):
        return {}


class AgentFrameworkBackend:
    """Runs a Microsoft Agent Framework agent in this same process, calling a
    Foundry model deployment directly. Foundry only ever sees inference
    calls - there's no Foundry-managed agent resource, so there's no
    versioning and no Entra Agent ID. provision()/latest_version() report the
    agent as always "provisioned", mirroring EchoBackend.

    Conversation memory: one Agent Framework session per conversation_id, kept
    in memory for the life of this process (not server-side persisted like
    Foundry's own conversations, so a worker restart loses in-flight chats).
    Created lazily on first use; nothing is evicted, so long-running processes
    accumulate one session per chat started.
    """

    supports_foundry_evaluation = False
    supports_agent_identity = False
    supports_local_evaluation = True  # evaluated locally with azure-ai-evaluation

    def __init__(self) -> None:
        # RLock: _agent() calls _chat_client() while already holding the lock,
        # and a plain Lock would deadlock on that same-thread re-acquisition.
        self._lock = threading.RLock()
        self._chat_clients: dict[str, Any] = {}
        self._agents: dict[str, Any] = {}
        self._sessions: dict[str, Any] = {}
        self._instrumented = False
        self._loop: Any = None
        self._loop_thread: threading.Thread | None = None

    def ensure_ready(self) -> None:
        from .client import get_clients

        get_clients()  # telemetry.configure() runs here, same as other backends
        self._ensure_instrumentation()

    def _ensure_loop(self):
        """One event loop for the life of this process, run on a dedicated
        background thread. Cached clients/agents/sessions hold async
        resources (HTTP connections) bound to whatever loop created them;
        calling asyncio.run() per invoke() would create and destroy a loop
        each time, so the second call would fail with "Event loop is closed"
        as soon as it touched a resource from the first call. Scheduling every
        call onto one persistent loop avoids that.
        """
        if self._loop is not None:
            return self._loop
        with self._lock:
            if self._loop is not None:
                return self._loop
            import asyncio

            loop = asyncio.new_event_loop()

            def _run_forever():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            thread = threading.Thread(target=_run_forever, daemon=True, name="agent-framework-loop")
            thread.start()
            self._loop = loop
            self._loop_thread = thread
            return self._loop

    def _run_async(self, coro):
        import asyncio

        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def _ensure_instrumentation(self) -> None:
        if self._instrumented:
            return
        with self._lock:
            if self._instrumented:
                return
            try:
                from agent_framework.observability import enable_instrumentation

                # Matches telemetry.py's AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED
                # default, so prompt/completion content is captured consistently
                # across backends.
                enable_instrumentation(enable_sensitive_data=True)
            except ImportError:
                pass
            self._instrumented = True

    def _chat_client(self, model: str):
        from .client import get_clients

        with self._lock:
            if model not in self._chat_clients:
                from agent_framework.foundry import FoundryChatClient

                self._chat_clients[model] = FoundryChatClient(
                    project_endpoint=settings.project_endpoint,
                    model=model,
                    credential=get_clients().credential,
                )
            return self._chat_clients[model]

    def _agent(self, name: str, model: str, instructions: str):
        with self._lock:
            if name not in self._agents:
                from agent_framework import Agent

                self._agents[name] = Agent(
                    client=self._chat_client(model), name=name, instructions=instructions
                )
            return self._agents[name]

    def _session(self, conversation_id: str, agent):
        with self._lock:
            if conversation_id not in self._sessions:
                self._sessions[conversation_id] = agent.create_session()
            return self._sessions[conversation_id]

    def provision(self, spec, name, model):
        return ProvisionedVersion(name=name, version="1")

    def latest_version(self, name):
        # The agent exists implicitly in code, so it is always "provisioned".
        return "1"

    def start_conversation(self):
        # The real Agent Framework session is created lazily on first invoke()
        # for this id (see _session()), since only invoke() knows which agent
        # and model it belongs to.
        return f"agentframework-{uuid.uuid4()}"

    @staticmethod
    def _extract_usage(result: Any) -> tuple[int, int, int]:
        """Best-effort token usage extraction; falls back to zeros.

        `result.usage_details` is a dict-like object (confirmed against a
        live Foundry model call), not a plain attribute-bearing object, so
        this checks both dict keys and attributes. Verify against your
        installed agent-framework version if the counts come back as zero.
        """
        usage = getattr(result, "usage_details", None) or getattr(result, "usage", None)
        if usage is None:
            return 0, 0, 0

        def _get(*keys: str) -> Any:
            for key in keys:
                if isinstance(usage, dict) and key in usage:
                    return usage[key]
                value = getattr(usage, key, None)
                if value is not None:
                    return value
            return None

        input_tokens = _get("input_token_count", "input_tokens") or 0
        output_tokens = _get("output_token_count", "output_tokens") or 0
        total_tokens = _get("total_token_count", "total_tokens") or (input_tokens + output_tokens)
        return int(input_tokens), int(output_tokens), int(total_tokens)

    def invoke(self, spec, name, prompt, conversation_id, model):
        agent = self._agent(name, model, spec.instructions)
        if conversation_id:
            session = self._session(conversation_id, agent)
            result = self._run_async(agent.run(prompt, session=session))
        else:
            # No conversation to continue (e.g. a one-off evaluation call).
            result = self._run_async(agent.run(prompt))

        input_tokens, output_tokens, total_tokens = self._extract_usage(result)
        return BackendResponse(
            output_text=str(getattr(result, "text", result)),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def span_attributes(self, name):
        return {}


_BACKENDS: dict[str, AgentBackend] = {
    "foundry_prompt": FoundryPromptBackend(),
    "echo": EchoBackend(),
    "agent_framework": AgentFrameworkBackend(),
}


def register_backend(name: str, backend: AgentBackend) -> None:
    _BACKENDS[name] = backend


def get_backend(name: str) -> AgentBackend:
    try:
        return _BACKENDS[name]
    except KeyError:
        raise KeyError(f"Unknown backend {name!r}; registered: {sorted(_BACKENDS)}") from None


def backend_names() -> list[str]:
    return sorted(_BACKENDS)


def load_plugins(modules: str | None = None) -> None:
    """Import plugin modules (comma-separated) so they can call register_backend().

    Defaults to the HARNESS_PLUGINS environment variable.
    """
    names = os.environ.get("HARNESS_PLUGINS", "") if modules is None else modules
    for name in filter(None, (n.strip() for n in names.split(","))):
        importlib.import_module(name)


load_plugins()
