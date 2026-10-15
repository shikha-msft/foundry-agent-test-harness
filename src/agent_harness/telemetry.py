"""OpenTelemetry wiring: traces, logs and token metrics -> App Insights -> Log Analytics."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from opentelemetry import metrics, trace

from .config import settings

_lock = threading.Lock()
_configured = False

logger = logging.getLogger("agent_harness")


def _resolve_connection_string(project_client=None) -> str | None:
    if settings.application_insights_connection_string:
        return settings.application_insights_connection_string
    if project_client is not None:
        # Falls back to the App Insights resource linked to the Foundry project.
        return project_client.telemetry.get_application_insights_connection_string()
    return None


def configure(project_client=None, credential=None) -> bool:
    """Configure OTel exactly once. Returns True if Azure Monitor export is active."""
    global _configured
    with _lock:
        if _configured:
            return True

        # Capture prompt/completion content on GenAI spans.
        os.environ.setdefault("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING", "true")
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
        os.environ.setdefault(
            "AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED", "true"
        )
        os.environ.setdefault("OTEL_SERVICE_NAME", settings.service_name)

        from azure.core.settings import settings as azure_settings

        azure_settings.tracing_implementation = "opentelemetry"

        connection_string = _resolve_connection_string(project_client)
        if not connection_string:
            logger.warning(
                "No App Insights connection string found; telemetry stays local."
            )
            _configured = True
            return False

        from azure.monitor.opentelemetry import configure_azure_monitor

        # Exports traces, logs and metrics to App Insights (workspace-based
        # -> rows land in the linked Log Analytics workspace). Passing a
        # credential authenticates ingestion with Entra ID, so the
        # instrumentation key in the connection string is not used.
        configure_azure_monitor(
            connection_string=connection_string,
            credential=credential,
            logger_name="agent_harness",
        )

        # Without this the INFO records below never reach the handler.
        logger.setLevel(logging.INFO)

        from azure.ai.projects.telemetry import AIProjectInstrumentor

        AIProjectInstrumentor().instrument()

        _configured = True
        return True


def get_tracer():
    return trace.get_tracer(settings.service_name)


_meter = None
_token_counter = None
_call_counter = None
_latency_histogram = None


def _ensure_instruments():
    global _meter, _token_counter, _call_counter, _latency_histogram
    if _meter is not None:
        return
    _meter = metrics.get_meter(settings.service_name)
    _token_counter = _meter.create_counter(
        "gen_ai.client.token.usage",
        unit="{token}",
        description="Tokens consumed per agent invocation",
    )
    _call_counter = _meter.create_counter(
        "gen_ai.client.invocations",
        unit="{call}",
        description="Agent invocations",
    )
    _latency_histogram = _meter.create_histogram(
        "gen_ai.client.operation.duration",
        unit="ms",
        description="Agent invocation latency",
    )


def record_usage(
    agent_name: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: float,
    status: str = "ok",
    extra_attributes: dict[str, Any] | None = None,
) -> None:
    """Emit token/latency metrics so they are queryable in Log Analytics.

    Extra attributes (identity, guardrail counts) go on the log record only,
    not on metric dimensions.
    """
    _ensure_instruments()
    base = {"gen_ai.agent.name": agent_name, "gen_ai.request.model": model}

    _token_counter.add(input_tokens, {**base, "gen_ai.token.type": "input"})
    _token_counter.add(output_tokens, {**base, "gen_ai.token.type": "output"})
    _call_counter.add(1, {**base, "status": status})
    _latency_histogram.record(duration_ms, base)

    # A structured log record gives an AppTraces row with the same dimensions,
    # which is easier to join in KQL than customMetrics alone.
    logger.info(
        "agent_invocation",
        extra={
            "agent_name": agent_name,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "duration_ms": round(duration_ms, 2),
            "status": status,
            **(extra_attributes or {}),
        },
    )


def flush(timeout_millis: int = 15000) -> None:
    """Force-export buffered telemetry. Short-lived scripts must call this."""
    from opentelemetry import metrics as _metrics
    from opentelemetry import trace as _trace

    providers = [_trace.get_tracer_provider(), _metrics.get_meter_provider()]

    try:
        from opentelemetry._logs import get_logger_provider

        providers.append(get_logger_provider())
    except Exception:  # logs API missing in this OTel build
        pass

    for provider in providers:
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is None:
            continue
        try:
            force_flush(timeout_millis)
        except Exception as exc:
            logger.debug("force_flush failed for %s: %s", type(provider).__name__, exc)
