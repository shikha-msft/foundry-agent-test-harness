"""Cloud red teaming against a demo agent via the Foundry AI Red Teaming Agent."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from azure.ai.projects.models import (
    AgentTaxonomyInput,
    AzureAIAgentTarget,
    EvaluationTaxonomy,
    RiskCategory,
)

from .agents import AGENTS_BY_KEY, backend_for, latest_version
from .client import get_clients
from .config import settings

DEFAULT_STRATEGIES = ["Flip", "Base64", "IndirectJailbreak"]


@dataclass
class RedTeamResult:
    agent_name: str
    agent_version: str
    eval_id: str
    run_id: str
    status: str
    strategies: list[str]
    num_turns: int
    result_counts: dict = field(default_factory=dict)
    items: list[dict] = field(default_factory=list)


def _criteria() -> list[dict]:
    return [
        {
            "type": "azure_ai_evaluator",
            "name": "Prohibited Actions",
            "evaluator_name": "builtin.prohibited_actions",
            "evaluator_version": "1",
        },
        {
            "type": "azure_ai_evaluator",
            "name": "Task Adherence",
            "evaluator_name": "builtin.task_adherence",
            "evaluator_version": "1",
            "initialization_parameters": {
                "deployment_name": settings.model_deployment
            },
        },
        {
            "type": "azure_ai_evaluator",
            "name": "Sensitive Data Leakage",
            "evaluator_name": "builtin.sensitive_data_leakage",
            "evaluator_version": "1",
        },
    ]


def run_redteam(
    agent_key: str,
    strategies: list[str] | None = None,
    num_turns: int = 3,
    poll_seconds: int = 15,
    timeout_seconds: int = 3600,
    on_status: Callable[[str], None] | None = None,
) -> RedTeamResult:
    strategies = strategies or DEFAULT_STRATEGIES
    spec = AGENTS_BY_KEY[agent_key]
    # Foundry's AI Red Teaming Agent service attacks a real Foundry-managed
    # agent resource; there's no local/SDK equivalent (unlike evaluations),
    # so this only works for backends that actually create one.
    if not backend_for(spec).supports_foundry_evaluation:
        raise RuntimeError(
            f"Agent {agent_key!r} uses backend {spec.backend!r}, which has no "
            "Foundry agent resource for the AI Red Teaming Agent to target."
        )
    version = latest_version(spec.name)
    if version is None:
        raise RuntimeError(
            f"Agent {spec.name!r} has no versions. Run provision_agents.py first."
        )

    clients = get_clients()
    target = AzureAIAgentTarget(name=spec.name, version=version)

    red_team = clients.openai.evals.create(
        name=f"foundry-agent-test-harness · {spec.name} · red-team",
        data_source_config={"type": "azure_ai_source", "scenario": "red_team"},
        testing_criteria=_criteria(),
    )

    # The taxonomy defines what "prohibited" means for this agent; the service
    # uses it to generate targeted attacks and score attack success rate.
    taxonomy = clients.project.beta.evaluation_taxonomies.create(
        name=spec.name,
        taxonomy=EvaluationTaxonomy(
            description=f"Red team taxonomy for {spec.name}",
            taxonomy_input=AgentTaxonomyInput(
                risk_categories=[RiskCategory.PROHIBITED_ACTIONS],
                target=target,
            ),
        ),
    )

    run = clients.openai.evals.runs.create(
        eval_id=red_team.id,
        name=f"red-team-run-{spec.name}",
        data_source={
            "type": "azure_ai_red_team",
            "item_generation_params": {
                "type": "red_team_taxonomy",
                "attack_strategies": strategies,
                "num_turns": num_turns,
                "source": {"type": "file_id", "id": taxonomy.id},
            },
            "target": target.as_dict(),
        },
    )

    deadline = time.time() + timeout_seconds
    while run.status not in ("completed", "failed", "canceled"):
        if time.time() > deadline:
            break
        if on_status:
            on_status(run.status)
        time.sleep(poll_seconds)
        run = clients.openai.evals.runs.retrieve(run_id=run.id, eval_id=red_team.id)

    items: list[dict] = []
    if run.status == "completed":
        for item in clients.openai.evals.runs.output_items.list(
            run_id=run.id, eval_id=red_team.id
        ):
            scores = {
                getattr(r, "name", "unknown"): {
                    "score": getattr(r, "score", None),
                    "passed": getattr(r, "passed", None),
                }
                for r in (getattr(item, "results", None) or [])
            }
            items.append(
                {
                    "attack": getattr(item, "datasource_item", None),
                    "scores": scores,
                }
            )

    return RedTeamResult(
        agent_name=spec.name,
        agent_version=version,
        eval_id=red_team.id,
        run_id=run.id,
        status=run.status,
        strategies=strategies,
        num_turns=num_turns,
        result_counts=_as_dict(getattr(run, "result_counts", None)),
        items=items,
    )


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
