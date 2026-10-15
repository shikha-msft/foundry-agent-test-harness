"""Batch driver - fires many prompts at one agent concurrently."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import telemetry
from .agents import AGENTS_BY_KEY, agent_name, backend_for
from .datasets import load_prompts
from .runtime import AgentResult, batch_budget, invoke

__all__ = ["BatchSummary", "load_prompts", "run_batch"]


@dataclass
class BatchSummary:
    batch_id: str
    trace_id: str
    agent_key: str
    total: int
    succeeded: int
    failed: int
    total_tokens: int
    input_tokens: int
    output_tokens: int
    avg_duration_ms: float
    flagged: int = 0
    results: list[AgentResult] = field(default_factory=list)


def run_batch(
    agent_key: str,
    prompts: Iterable[str],
    max_workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    model: str | None = None,
) -> BatchSummary:
    prompts = list(prompts)
    batch_id = str(uuid.uuid4())[:8]
    spec = AGENTS_BY_KEY[agent_key]
    budget = batch_budget(agent_key)

    # Configure telemetry before the first span, otherwise the global tracer
    # provider is still the no-op one and spans get a zero trace id.
    backend_for(spec).ensure_ready()
    tracer = telemetry.get_tracer()

    results: list[AgentResult] = []

    with tracer.start_as_current_span("agent_harness.batch") as root:
        root.set_attribute("agent_harness.batch_id", batch_id)
        root.set_attribute("agent_harness.batch_size", len(prompts))
        root.set_attribute("gen_ai.agent.name", agent_name(spec, model))
        trace_id = f"{root.get_span_context().trace_id:032x}"

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(
                    invoke,
                    agent_key,
                    prompt,
                    None,
                    {"agent_harness.batch_id": batch_id},
                    model,
                    budget,
                )
                for prompt in prompts
            ]
            for done, future in enumerate(as_completed(futures), start=1):
                results.append(future.result())
                if on_progress:
                    on_progress(done, len(prompts))

        input_tokens = sum(r.input_tokens for r in results)
        output_tokens = sum(r.output_tokens for r in results)
        failed = sum(1 for r in results if not r.succeeded)
        flagged = sum(1 for r in results if r.status == "flagged")
        durations = [r.duration_ms for r in results] or [0.0]

        root.set_attribute("agent_harness.batch_total_tokens", input_tokens + output_tokens)
        root.set_attribute("agent_harness.batch_failed", failed)
        root.set_attribute("agent_harness.batch_flagged", flagged)

        return BatchSummary(
            batch_id=batch_id,
            trace_id=trace_id,
            agent_key=agent_key,
            total=len(results),
            succeeded=len(results) - failed,
            failed=failed,
            total_tokens=input_tokens + output_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            avg_duration_ms=sum(durations) / len(durations),
            flagged=flagged,
            results=results,
        )
