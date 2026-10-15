"""Microsoft Entra Agent ID for the demo agents.

Foundry provisions the Entra objects itself: every agent created under the
current agent object model gets its own agent identity blueprint and agent
identity (`blueprint` / `instance_identity` on the agent). Agents created
before that model are "legacy": those fields are null and the agent runs as the
project's shared agent identity. A legacy agent can't be upgraded in place, so
recreate_legacy() deletes it and recreates it from the same definition.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from azure.core.exceptions import ResourceNotFoundError

from .agents import AgentSpec, agent_name, provision
from .client import get_clients

# Roles that are never needed by a prompt agent and would give it control over
# resources or other identities. A tool-less prompt agent needs no roles at all.
PRIVILEGED_ROLES = frozenset(
    {
        "Owner",
        "Contributor",
        "User Access Administrator",
        "Role Based Access Control Administrator",
        "Azure AI Account Owner",
        "Foundry Account Owner",
        "Azure AI Owner",
        "Foundry Owner",
        "Cognitive Services Contributor",
        "Key Vault Administrator",
        "Storage Account Contributor",
    }
)


@dataclass(frozen=True)
class EntraIdentity:
    principal_id: str
    client_id: str
    status: str | None = None


@dataclass(frozen=True)
class AgentIdentityInfo:
    agent_name: str
    agent_id: str | None
    state: str | None
    state_source: str | None
    instance: EntraIdentity | None
    blueprint: EntraIdentity | None
    blueprint_id: str | None = None
    role_assignments: list[dict[str, str]] = field(default_factory=list)

    @property
    def is_legacy(self) -> bool:
        """No distinct identity: the agent runs as the shared project identity."""
        return self.instance is None

    @property
    def privileged_roles(self) -> list[str]:
        return sorted(
            {a["role"] for a in self.role_assignments if a["role"] in PRIVILEGED_ROLES}
        )

    def span_attributes(self) -> dict[str, str]:
        attrs: dict[str, str] = {
            "entra.agent_identity.type": "shared_project" if self.is_legacy else "distinct"
        }
        if self.agent_id:
            attrs["gen_ai.agent.id"] = self.agent_id
        if self.instance:
            attrs["entra.agent_identity.client_id"] = self.instance.client_id
            attrs["entra.agent_identity.principal_id"] = self.instance.principal_id
        if self.blueprint:
            attrs["entra.agent_blueprint.client_id"] = self.blueprint.client_id
        return attrs

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["is_legacy"] = self.is_legacy
        data["privileged_roles"] = self.privileged_roles
        return data


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(getattr(value, "value", value))


def _entra(value: Any) -> EntraIdentity | None:
    if value is None:
        return None
    principal_id = getattr(value, "principal_id", None)
    client_id = getattr(value, "client_id", None)
    if not principal_id and not client_id:
        return None
    return EntraIdentity(
        principal_id=principal_id or "",
        client_id=client_id or "",
        status=_text(getattr(value, "status", None)),
    )


def get_identity(name: str) -> AgentIdentityInfo | None:
    """Read the agent's Entra Agent ID from Foundry. None if the agent doesn't exist."""
    clients = get_clients()
    try:
        agent = clients.project.agents.get(agent_name=name)
    except ResourceNotFoundError:
        return None

    blueprint_ref = getattr(agent, "blueprint_reference", None)
    return AgentIdentityInfo(
        agent_name=agent.name,
        agent_id=agent.id,
        state=_text(getattr(agent, "state", None)),
        state_source=_text(getattr(agent, "state_source", None)),
        instance=_entra(getattr(agent, "instance_identity", None)),
        blueprint=_entra(getattr(agent, "blueprint", None)),
        blueprint_id=getattr(blueprint_ref, "blueprint_id", None),
    )


_cache: dict[str, AgentIdentityInfo | None] = {}
_cache_lock = threading.Lock()


def cached_identity(name: str) -> AgentIdentityInfo | None:
    """Identity lookup for the hot path. Identities don't change between turns,
    and a failed lookup must never break an agent call."""
    with _cache_lock:
        if name in _cache:
            return _cache[name]
    try:
        info = get_identity(name)
    except Exception:
        info = None
    with _cache_lock:
        _cache[name] = info
    return info


def forget(name: str) -> None:
    with _cache_lock:
        _cache.pop(name, None)


def recreate_legacy(spec: AgentSpec, model: str | None = None) -> AgentIdentityInfo | None:
    """Give a legacy agent a distinct Entra Agent ID by recreating it.

    Destructive: deletes every version of the agent. The new agent keeps the
    same name and definition, so callers that use the name are unaffected, but
    Entra object IDs, version numbers and any role assignments made to the
    shared identity do not carry over.
    """
    name = agent_name(spec, model)
    current = get_identity(name)
    if current is not None and not current.is_legacy:
        return current

    clients = get_clients()
    if current is not None:
        clients.project.agents.delete(agent_name=name)
    provision(spec, model)
    forget(name)
    return get_identity(name)


def role_assignments(principal_id: str) -> list[dict[str, str]]:
    """Azure RBAC assignments held by an agent identity, via the Azure CLI.

    Only assignments the caller can read are returned (the signed-in identity
    needs Microsoft.Authorization/roleAssignments/read at the relevant scopes).
    """
    az = shutil.which("az")
    if az is None:
        raise RuntimeError("Azure CLI not found; it's needed to list role assignments.")
    completed = subprocess.run(
        [
            az, "role", "assignment", "list",
            "--assignee", principal_id,
            "--all",
            "--query", "[].{role:roleDefinitionName, scope:scope}",
            "--output", "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "az role assignment list failed")
    return json.loads(completed.stdout or "[]")
