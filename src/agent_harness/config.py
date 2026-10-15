"""Central configuration loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

# Local .env only; real environment variables win over it.
for candidate in (PACKAGE_ROOT / ".env",):
    if candidate.exists():
        load_dotenv(dotenv_path=candidate, override=False)
        # Blank "KEY=" lines in .env mean "unset" (see .env.example), but some
        # SDKs (for example azure-monitor-opentelemetry's exporter) read
        # os.environ directly and crash on an empty-but-present value instead
        # of a missing one. Drop any key .env set to "" so it reads as unset.
        for key, value in dotenv_values(candidate).items():
            if value == "" and os.environ.get(key) == "":
                os.environ.pop(key, None)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class Settings:
    project_endpoint: str
    model_deployment: str
    service_name: str = "foundry-agent-test-harness"
    application_insights_connection_string: str | None = None
    log_analytics_workspace_id: str | None = None
    agents_dir: Path = PACKAGE_ROOT / "agents"

    @classmethod
    def load(cls) -> "Settings":
        agents_dir = os.environ.get("HARNESS_AGENTS_DIR")
        return cls(
            project_endpoint=_require("AZURE_AI_PROJECT_ENDPOINT"),
            model_deployment=_require("AZURE_AI_MODEL_DEPLOYMENT_NAME"),
            service_name=os.environ.get("OTEL_SERVICE_NAME", "foundry-agent-test-harness"),
            application_insights_connection_string=os.environ.get(
                "APPLICATIONINSIGHTS_CONNECTION_STRING"
            ),
            log_analytics_workspace_id=os.environ.get("LOG_ANALYTICS_WORKSPACE_ID"),
            agents_dir=Path(agents_dir) if agents_dir else PACKAGE_ROOT / "agents",
        )


settings = Settings.load()
