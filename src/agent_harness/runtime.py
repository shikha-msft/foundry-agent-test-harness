"""The harness call path. Every agent call, from chat, batch or scripts, goes:

    input guardrails -> token budget -> backend (timeout + retries)
      -> output guardrails -> spans, metrics and logs

Result statuses: ok, flagged (output guardrail in flag mode), blocked
(a guardrail in block mode), budget_exceeded, error.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from opentelemetry.trace import Status, StatusCode

from . import guardrails, telemetry
from .agents import AGENTS_BY_KEY, AgentSpec, agent_name, backend_for
from .config import settings
from .limits import TokenBudget

OK_STATUSES = ("ok", "flagged")


@dataclass
class AgentResult:
    agent_key: str
    agent_name: str
    model: str
    prompt: str
    output_text: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    duration_ms: float
    trace_id: str
    status: str = "ok"
    error: str | None = None
    guardrail_violations: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status in OK_STATUSES

    def to_dict(self) -> dict:
        return asdict(self)


def session_budget(agent_key: str) -> TokenBudget:
    spec = AGENTS_BY_KEY[agent_key]
    return TokenBudget(spec.limits.session_token_budget, scope="session")


def batch_budget(agent_key: str) -> TokenBudget:
    spec = AGENTS_BY_KEY[agent_key]
    return TokenBudget(spec.limits.batch_token_budget, scope="batch")


def invoke(
    agent_key: str,
    prompt: str,
    conversation_id: str | None = None,
    attributes: dict[str, Any] | None = None,
    model: str | None = None,
    budget: TokenBudget | None = None,
) -> AgentResult:
    """Invoke one agent through the harness, emitting a span and token metrics."""
    spec: AgentSpec = AGENTS_BY_KEY[agent_key]
    model = model or settings.model_deployment
    name = agent_name(spec, model)
    backend = backend_for(spec)
    backend.ensure_ready()
    tracer = telemetry.get_tracer()
    try:
        extra = backend.span_attributes(name)
    except Exception:
        extra = {}

    with tracer.start_as_current_span(f"invoke_agent {name}") as span:
        span.set_attribute("gen_ai.agent.name", name)
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("agent_harness.agent_key", agent_key)
        span.set_attribute("harness.backend", spec.backend)
        for key, value in {**extra, **(attributes or {})}.items():
            span.set_attribute(key, value)

        trace_id = f"{span.get_span_context().trace_id:032x}"
        started = time.perf_counter()
        violations: list[str] = []

        def finish(
            status: str,
            output_text: str = "",
            tokens: tuple[int, int, int] = (0, 0, 0),
            error: str | None = None,
        ) -> AgentResult:
            duration_ms = (time.perf_counter() - started) * 1000
            input_tokens, output_tokens, total_tokens = tokens
            span.set_attribute("harness.status", status)
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
            span.set_attribute("gen_ai.usage.total_tokens", total_tokens)
            span.set_attribute("agent_harness.duration_ms", round(duration_ms, 2))
            if violations:
                span.set_attribute("harness.guardrail.violations", violations)
            if budget is not None:
                span.set_attribute(f"harness.budget.{budget.scope}_used", budget.used)
            if status == "error":
                span.set_status(Status(StatusCode.ERROR, error or "error"))
            telemetry.record_usage(
                name,
                model,
                input_tokens,
                output_tokens,
                duration_ms,
                status=status,
                extra_attributes={
                    **extra,
                    "guardrail_violations": len(violations),
                },
            )
            return AgentResult(
                agent_key=agent_key,
                agent_name=name,
                model=model,
                prompt=prompt,
                output_text=output_text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                duration_ms=duration_ms,
                trace_id=trace_id,
                status=status,
                error=error,
                guardrail_violations=list(violations),
            )

        pre = guardrails.check("input", prompt, spec)
        violations.extend(pre.violations)
        if pre.blocked:
            return finish("blocked", error=guardrails.BLOCKED_INPUT_MESSAGE)

        if budget is not None and budget.exhausted():
            return finish(
                "budget_exceeded",
                error=f"{budget.scope} token budget of {budget.limit} is used up",
            )

        try:
            response = backend.invoke(spec, name, prompt, conversation_id, model)
        except Exception as exc:
            span.record_exception(exc)
            return finish("error", error=str(exc))

        tokens = (response.input_tokens, response.output_tokens, response.total_tokens)
        if budget is not None:
            budget.consume(response.total_tokens)

        post = guardrails.check("output", response.output_text, spec)
        violations.extend(post.violations)
        if post.blocked:
            return finish("blocked", tokens=tokens, error=guardrails.BLOCKED_OUTPUT_MESSAGE)
        status = "flagged" if post.violations else "ok"
        return finish(status, output_text=response.output_text, tokens=tokens)


def start_conversation(agent_key: str) -> str | None:
    """Server-side conversation, so the agent keeps context across chat turns."""
    return backend_for(AGENTS_BY_KEY[agent_key]).start_conversation()


def chat_turn(
    agent_key: str,
    message: str,
    conversation_id: str | None = None,
    session_id: str | None = None,
    model: str | None = None,
    budget: TokenBudget | None = None,
) -> AgentResult:
    """One chat turn: a root span per turn, with the agent call nested inside."""
    session_id = session_id or str(uuid.uuid4())
    spec = AGENTS_BY_KEY[agent_key]
    # Telemetry must be live before the root span is created.
    backend_for(spec).ensure_ready()
    tracer = telemetry.get_tracer()

    with tracer.start_as_current_span("agent_harness.chat_turn") as root:
        root.set_attribute("agent_harness.session_id", session_id)
        root.set_attribute("agent_harness.selected_agent", agent_name(spec, model))
        if conversation_id:
            root.set_attribute("agent_harness.conversation_id", conversation_id)

        result = invoke(
            agent_key,
            message,
            conversation_id=conversation_id,
            attributes={"agent_harness.session_id": session_id},
            model=model,
            budget=budget,
        )

        root.set_attribute("agent_harness.total_tokens", result.total_tokens)
        root.set_attribute("agent_harness.status", result.status)
        return result
