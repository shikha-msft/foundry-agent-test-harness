"""Agent registry: specs come from YAML (see specs.py), operations go through
each spec's backend (see backends.py).

The Streamlit app creates each agent once, on first load, via
ensure_provisioned(): it creates an agent only when none exists and never adds
a version to an existing one. scripts/provision_agents.py is the manual way to
add a new version on purpose, for example after changing an agent's
instructions.
"""

from __future__ import annotations

from .backends import AgentBackend, get_backend
from .client import get_clients
from .config import settings
from .specs import AgentSpec, load_specs

AGENT_SPECS: list[AgentSpec] = load_specs()
AGENTS_BY_KEY = {spec.key: spec for spec in AGENT_SPECS}

__all__ = [
    "AGENT_SPECS",
    "AGENTS_BY_KEY",
    "AgentSpec",
    "agent_name",
    "available_models",
    "backend_for",
    "ensure_provisioned",
    "latest_version",
    "provision",
    "provision_all",
]


def backend_for(spec: AgentSpec) -> AgentBackend:
    return get_backend(spec.backend)


def _slug(model: str) -> str:
    return model.replace(".", "-").replace("_", "-").lower()


def agent_name(spec: AgentSpec, model: str | None = None) -> str:
    """One agent per model. The model lives in the name, never in the version,
    so switching models in the UI never re-versions an agent."""
    return f"{spec.name}-{_slug(model or settings.model_deployment)}"


def provision(spec: AgentSpec, model: str | None = None):
    """Create a new immutable version of one agent."""
    model = model or settings.model_deployment
    return backend_for(spec).provision(spec, agent_name(spec, model), model)


def provision_all(model: str | None = None) -> list[dict]:
    model = model or settings.model_deployment
    results = []
    for spec in AGENT_SPECS:
        agent = provision(spec, model)
        results.append(
            {
                "key": spec.key,
                "name": agent.name,
                "version": agent.version,
                "id": agent.id,
                "model": model,
                "backend": spec.backend,
            }
        )
    return results


def latest_version(agent_name_: str, spec: AgentSpec | None = None) -> str | None:
    backend = backend_for(spec) if spec else get_backend("foundry_prompt")
    return backend.latest_version(agent_name_)


def available_models() -> list[str]:
    """Chat deployments on the account. Embeddings can't back a prompt agent."""
    try:
        names = [
            d.name
            for d in get_clients().project.deployments.list()
            if "embedding" not in f"{d.name} {getattr(d, 'model_name', '')}".lower()
        ]
    except Exception:
        names = []
    if settings.model_deployment not in names:
        names.append(settings.model_deployment)
    return sorted(names)


def ensure_provisioned(model: str | None = None) -> dict[str, str]:
    """Create this model's agents if they don't exist yet. Never adds a version
    to an existing agent, so repeated model switching is stable."""
    model = model or settings.model_deployment
    versions: dict[str, str] = {}
    for spec in AGENT_SPECS:
        existing = latest_version(agent_name(spec, model), spec)
        versions[spec.key] = existing or provision(spec, model).version
    return versions
