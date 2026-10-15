"""Foundry evaluations (quality + safety) for the demo agents."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from openai.types.eval_create_params import DataSourceConfigCustom

from .agents import AGENTS_BY_KEY, agent_name, backend_for, latest_version
from .client import get_clients
from .config import settings
from .datasets import load_prompts

ITEM_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


def _quality_criteria(model: str) -> list[dict]:
    return [
        {
            "type": "azure_ai_evaluator",
            "name": "fluency",
            "evaluator_name": "builtin.fluency",
            "initialization_parameters": {"deployment_name": model},
            "data_mapping": {
                "query": "{{item.query}}",
                "response": "{{sample.output_text}}",
            },
        },
        {
            "type": "azure_ai_evaluator",
            "name": "coherence",
            "evaluator_name": "builtin.coherence",
            "initialization_parameters": {"deployment_name": model},
            "data_mapping": {
                "query": "{{item.query}}",
                "response": "{{sample.output_text}}",
            },
        },
        {
            "type": "azure_ai_evaluator",
            "name": "task_adherence",
            "evaluator_name": "builtin.task_adherence",
            "initialization_parameters": {"deployment_name": model},
            # Docs: task_adherence needs full interaction context, not plain text.
            "data_mapping": {
                "query": "{{item.query}}",
                "response": "{{sample.output_items}}",
            },
        },
    ]


def _safety_criteria(model: str) -> list[dict]:
    # Safety evaluators use the managed content-safety model, so no deployment name.
    return [
        {
            "type": "azure_ai_evaluator",
            "name": name,
            "evaluator_name": f"builtin.{name}",
            "data_mapping": {
                "query": "{{item.query}}",
                "response": "{{sample.output_text}}",
            },
        }
        for name in ("violence", "hate_unfairness", "self_harm")
    ]


CRITERIA = {"quality": _quality_criteria, "safety": _safety_criteria}


@dataclass
class EvaluationResult:
    kind: str
    agent_name: str
    agent_version: str
    eval_id: str
    run_id: str
    status: str
    result_counts: dict = field(default_factory=dict)
    items: list[dict] = field(default_factory=list)


def load_queries(
    path: str | Path, limit: int | None = None, agent_key: str | None = None
) -> list[str]:
    return load_prompts(path, agent_key=agent_key, limit=limit)


def run_evaluation(
    agent_key: str,
    queries: list[str],
    kind: str = "quality",
    poll_seconds: int = 5,
    timeout_seconds: int = 900,
    on_status: Callable[[str], None] | None = None,
    model: str | None = None,
) -> EvaluationResult:
    """Evaluate an agent. Dispatches to Foundry's server-side eval-run service
    for backends with a real Foundry agent resource (`foundry_prompt`), or to
    `_run_local_evaluation` for backends that support local evaluation instead
    (for example `agent_framework`) - same evaluator types either way, so pass
    rates are comparable. Backends with neither (for example `echo`, an
    offline stub) raise, same as before.
    """
    if kind not in CRITERIA:
        raise ValueError(f"kind must be one of {sorted(CRITERIA)}")

    model = model or settings.model_deployment
    spec = AGENTS_BY_KEY[agent_key]
    backend = backend_for(spec)
    if not backend.supports_foundry_evaluation:
        if not backend.supports_local_evaluation:
            raise RuntimeError(
                f"Agent {agent_key!r} uses backend {spec.backend!r}, which can't "
                "be evaluated (no Foundry agent resource and no local evaluation "
                "support)."
            )
        return _run_local_evaluation(
            agent_key, queries, kind, model=model, on_status=on_status
        )
    name = agent_name(spec, model)
    version = latest_version(name, spec)
    if version is None:
        raise RuntimeError(
            f"Agent {name!r} has no versions. Run provision_agents.py first."
        )

    clients = get_clients()

    evaluation = clients.openai.evals.create(
        name=f"foundry-agent-test-harness · {name} · {kind}",
        data_source_config=DataSourceConfigCustom(
            type="custom", item_schema=ITEM_SCHEMA, include_sample_schema=True
        ),
        testing_criteria=CRITERIA[kind](model),
    )

    run = clients.openai.evals.runs.create(
        eval_id=evaluation.id,
        name=f"{kind}-run-{name}",
        data_source={
            "type": "azure_ai_target_completions",
            "source": {
                "type": "file_content",
                "content": [{"item": {"query": q}} for q in queries],
            },
            "input_messages": {
                "type": "template",
                "template": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": {"type": "input_text", "text": "{{item.query}}"},
                    }
                ],
            },
            "target": {
                "type": "azure_ai_agent",
                "name": name,
                "version": version,
            },
        },
    )

    deadline = time.time() + timeout_seconds
    while run.status not in ("completed", "failed", "canceled"):
        if time.time() > deadline:
            break
        if on_status:
            on_status(run.status)
        time.sleep(poll_seconds)
        run = clients.openai.evals.runs.retrieve(run_id=run.id, eval_id=evaluation.id)

    items: list[dict] = []
    if run.status == "completed":
        for item in clients.openai.evals.runs.output_items.list(
            run_id=run.id, eval_id=evaluation.id
        ):
            scores = {
                getattr(r, "name", "unknown"): {
                    "score": getattr(r, "score", None),
                    "passed": getattr(r, "passed", None),
                    "label": getattr(r, "label", None),
                    "reason": getattr(r, "reason", None),
                }
                for r in (getattr(item, "results", None) or [])
            }
            items.append(
                {
                    "query": (getattr(item, "datasource_item", None) or {}).get("query"),
                    "scores": scores,
                }
            )

    return EvaluationResult(
        kind=kind,
        agent_name=name,
        agent_version=version,
        eval_id=evaluation.id,
        run_id=run.id,
        status=run.status,
        result_counts=_as_dict(getattr(run, "result_counts", None)),
        items=items,
    )


def _account_endpoint(project_endpoint: str) -> str:
    """The Foundry account's own root endpoint, derived from the project
    endpoint (".../api/projects/<project>" -> the account root). The Azure AI
    Evaluation SDK's quality evaluators need this OpenAI-compatible endpoint,
    not the project endpoint.
    """
    parts = urlsplit(project_endpoint)
    return f"{parts.scheme}://{parts.netloc}/"


def _local_quality_evaluators(model: str) -> dict[str, Any]:
    from azure.ai.evaluation import (
        AzureOpenAIModelConfiguration,
        CoherenceEvaluator,
        FluencyEvaluator,
        TaskAdherenceEvaluator,
    )

    model_config = AzureOpenAIModelConfiguration(
        azure_deployment=model,
        azure_endpoint=_account_endpoint(settings.project_endpoint),
        api_version="2025-04-01-preview",
    )
    # gpt-5-class ("reasoning") models reject the legacy max_tokens parameter;
    # is_reasoning_model=True tells the evaluator to send max_completion_tokens
    # instead. Verified live against a gpt-5-mini deployment - without this,
    # every quality evaluator call fails with a 400 BadRequestError.
    return {
        "fluency": FluencyEvaluator(model_config=model_config, is_reasoning_model=True),
        "coherence": CoherenceEvaluator(model_config=model_config, is_reasoning_model=True),
        "task_adherence": TaskAdherenceEvaluator(
            model_config=model_config, is_reasoning_model=True
        ),
    }


def _local_safety_evaluators(credential: Any) -> dict[str, Any]:
    from azure.ai.evaluation import HateUnfairnessEvaluator, SelfHarmEvaluator, ViolenceEvaluator

    # Safety evaluators call Foundry's content-safety service via the project
    # reference - no Foundry agent resource needed, just the project.
    kwargs = {"azure_ai_project": settings.project_endpoint, "credential": credential}
    return {
        "violence": ViolenceEvaluator(**kwargs),
        "hate_unfairness": HateUnfairnessEvaluator(**kwargs),
        "self_harm": SelfHarmEvaluator(**kwargs),
    }


def _run_local_evaluation(
    agent_key: str,
    queries: list[str],
    kind: str,
    model: str,
    on_status: Callable[[str], None] | None = None,
) -> EvaluationResult:
    """Evaluate a backend with no Foundry agent resource (for example
    `agent_framework`) with the Azure AI Evaluation SDK's local evaluators,
    instead of Foundry's server-side eval-run service. Same evaluator types as
    `CRITERIA` (fluency/coherence/task_adherence for quality,
    violence/hate_unfairness/self_harm for safety), so pass rates are
    comparable to a Foundry-run evaluation - just computed in this process,
    one query at a time, instead of server-side on a dataset file.
    """
    spec = AGENTS_BY_KEY[agent_key]
    backend = backend_for(spec)
    backend.ensure_ready()
    name = agent_name(spec, model)

    clients = get_clients()
    evaluators = (
        _local_quality_evaluators(model)
        if kind == "quality"
        else _local_safety_evaluators(clients.credential)
    )

    items: list[dict] = []
    for i, query in enumerate(queries):
        if on_status:
            on_status(f"evaluating {i + 1}/{len(queries)}")
        response = backend.invoke(spec, name, query, None, model)
        scores: dict[str, dict] = {}
        for eval_name, evaluator in evaluators.items():
            try:
                raw = evaluator(query=query, response=response.output_text)
            except Exception as exc:
                scores[eval_name] = {
                    "score": None,
                    "passed": None,
                    "label": None,
                    "reason": str(exc),
                }
                continue
            passed = raw.get(f"{eval_name}_passed")
            if passed is None and f"{eval_name}_result" in raw:
                passed = raw.get(f"{eval_name}_result") == "pass"
            scores[eval_name] = {
                "score": raw.get(f"{eval_name}_score"),
                "passed": passed,
                "label": raw.get(eval_name),
                "reason": raw.get(f"{eval_name}_reason"),
            }
        items.append({"query": query, "scores": scores})

    return EvaluationResult(
        kind=kind,
        agent_name=name,
        agent_version="n/a (agent_framework has no Foundry agent version)",
        eval_id="local",
        run_id=f"local-{uuid.uuid4()}",
        status="completed",
        result_counts={},
        items=items,
    )


def pass_rate(result: "EvaluationResult") -> float:
    """Fraction of evaluator checks that passed, across all items."""
    checks = [
        score.get("passed")
        for item in result.items
        for score in item["scores"].values()
        if score.get("passed") is not None
    ]
    if not checks:
        return 0.0
    return sum(1 for c in checks if c) / len(checks)


def _as_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {
        k: getattr(value, k)
        for k in ("passed", "failed", "errored", "total")
        if hasattr(value, k)
    }
