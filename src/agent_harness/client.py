"""Foundry client factory. Telemetry is configured before any agent call is made."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from . import telemetry
from .config import settings

_lock = threading.Lock()
_bundle: "Clients | None" = None


@dataclass
class Clients:
    project: AIProjectClient
    openai: Any
    credential: DefaultAzureCredential
    telemetry_enabled: bool


def build_credential() -> DefaultAzureCredential:
    """`az` can be slow to start here, so allow a longer subprocess timeout.

    The azd credential is excluded because a stale azd login in another tenant
    makes it fail noisily before the CLI credential is reached.
    """
    return DefaultAzureCredential(
        process_timeout=60,
        exclude_developer_cli_credential=True,
    )


def get_clients() -> Clients:
    """Process-wide singleton so Streamlit reruns don't leak connections."""
    global _bundle
    with _lock:
        if _bundle is not None:
            return _bundle

        credential = build_credential()
        project = AIProjectClient(
            endpoint=settings.project_endpoint, credential=credential
        )
        telemetry_enabled = telemetry.configure(project, credential)
        openai = project.get_openai_client()

        _bundle = Clients(
            project=project,
            openai=openai,
            credential=credential,
            telemetry_enabled=telemetry_enabled,
        )
        return _bundle
