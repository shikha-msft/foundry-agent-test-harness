"""Read Foundry's native platform logs back out of the Log Analytics workspace."""

from __future__ import annotations

import re
from datetime import timedelta

import pandas as pd
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

from .client import build_credential
from .config import settings

# Queries containing this marker can be scoped to one agent; the rest are not
# agent-attributed (Foundry's own spans carry the model, not the agent).
AGENT_FILTER = "{agent_filter}"

_AGENT_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# Foundry emits these itself (ServiceName "responsesapi") into the App Insights
# linked to the project - distinct from this app's own agent_invocation logs.
# AppDependencies has no agent/model columns, so they are lifted out of the
# Properties bag before filtering, keeping the agent filter uniform.
_SPAN_ATTRS = """| extend p = parse_json(Properties)
| extend AgentName = tostring(p["gen_ai.agent.name"]),
         ModelName = tostring(p["gen_ai.request.model"])"""

QUERIES: dict[str, str] = {
    "Total tokens and runs": """
AppGenAIContent
{agent_filter}
| extend a = parse_json(Attributes)
| extend input_tokens = toint(a["gen_ai.usage.input_tokens"]),
         output_tokens = toint(a["gen_ai.usage.output_tokens"]),
         cached_tokens = toint(a["gen_ai.usage.cached_tokens"])
| extend run_tokens = input_tokens + output_tokens
| summarize total_runs = count(),
            total_input_tokens = sum(input_tokens),
            total_output_tokens = sum(output_tokens),
            total_tokens = sum(run_tokens),
            total_cached_tokens = sum(cached_tokens)
        by agent = AgentName, model = ModelName
| order by total_tokens desc
""",
    "Agent runs": """
AppGenAIContent
{agent_filter}
| extend a = parse_json(Attributes)
| project TimeGenerated,
          agent = AgentName,
          model = ModelName,
          agent_version = AgentId,
          input_tokens = toint(a["gen_ai.usage.input_tokens"]),
          output_tokens = toint(a["gen_ai.usage.output_tokens"]),
          total_tokens = toint(a["gen_ai.usage.input_tokens"]) + toint(a["gen_ai.usage.output_tokens"]),
          cached_tokens = toint(a["gen_ai.usage.cached_tokens"]),
          finish = tostring(a["gen_ai.response.finish_reasons"]),
          TraceId
| order by TimeGenerated desc
""",
    "Prompts and completions": """
AppGenAIContent
{agent_filter}
| project TimeGenerated, agent = AgentName, model = ModelName,
          agent_version = AgentId,
          SystemInstructions, InputMessages, OutputMessages
| order by TimeGenerated desc
""",
    "Token usage by agent": """
AppGenAIContent
{agent_filter}
| extend a = parse_json(Attributes)
| extend input_tokens = toint(a["gen_ai.usage.input_tokens"]),
         output_tokens = toint(a["gen_ai.usage.output_tokens"]),
         cached_tokens = toint(a["gen_ai.usage.cached_tokens"])
| extend total_tokens = input_tokens + output_tokens
| summarize input_tokens = sum(input_tokens),
            output_tokens = sum(output_tokens),
            total_tokens = sum(total_tokens),
            cached_tokens = sum(cached_tokens),
            runs = count()
        by agent = AgentName, model = ModelName, bin(TimeGenerated, 5m)
| order by TimeGenerated desc
""",
    "Tool calls": """
AppGenAIContent
{agent_filter}
| where isnotempty(ToolCallArguments) or isnotempty(ToolDefinitions)
| project TimeGenerated, agent = AgentName, model = ModelName,
          ToolDefinitions, ToolCallArguments, ToolCallResult
| order by TimeGenerated desc
""",
    "Foundry spans": f"""
AppDependencies
| where column_ifexists("AppRoleName", "") == "responsesapi"
{_SPAN_ATTRS}
{{agent_filter}}
| project TimeGenerated, agent = AgentName, model = ModelName,
          Name, DurationMs, Success, OperationId, ParentId
| order by TimeGenerated desc
""",
    "Evaluation results": """
AppEvents
| where Name == "gen_ai.evaluation.result"
| extend p = parse_json(Properties)
| extend AgentName = tostring(p["gen_ai.agent.name"])
{agent_filter}
| project TimeGenerated,
          agent = AgentName,
          agent_version = tostring(p["gen_ai.agent.id"]),
          evaluator = tostring(p["gen_ai.evaluation.name"]),
          score = todouble(p["gen_ai.evaluation.score.value"]),
          result = tostring(p["gen_ai.evaluation.score.label"]),
          explanation = tostring(p["gen_ai.evaluation.explanation"])
| order by TimeGenerated desc
""",
    "Content safety": f"""
AppDependencies
{_SPAN_ATTRS}
{{agent_filter}}
| where isnotempty(tostring(p["microsoft.foundry.content_filter.results"]))
| extend cf = parse_json(tostring(p["microsoft.foundry.content_filter.results"]))
| mv-expand cf
| extend r = cf.content_filter_results
| project TimeGenerated, agent = AgentName, model = ModelName,
          source = tostring(cf.source_type),
          blocked = tostring(cf.blocked),
          hate = tostring(r.hate.severity),
          sexual = tostring(r.sexual.severity),
          violence = tostring(r.violence.severity),
          self_harm = tostring(r.self_harm.severity),
          jailbreak = tostring(r.jailbreak.detected),
          indirect_attack = tostring(r.indirect_attack.detected)
| order by TimeGenerated desc
""",
    "Errors": f"""
AppDependencies
| where Success == false
{_SPAN_ATTRS}
{{agent_filter}}
| project TimeGenerated, agent = AgentName, model = ModelName,
          Name, ResultCode, OperationId
| order by TimeGenerated desc
""",
    # The Entra Agent ID attributes are added by this app (runtime.invoke), so
    # unlike the queries above this one reads the app's own spans.
    "Entra Agent ID": f"""
AppDependencies
{_SPAN_ATTRS}
| where isnotempty(tostring(p["entra.agent_identity.type"]))
{{agent_filter}}
| summarize calls = count(), last_seen = max(TimeGenerated)
        by agent = AgentName,
           identity_type = tostring(p["entra.agent_identity.type"]),
           agent_identity_client_id = tostring(p["entra.agent_identity.client_id"]),
           agent_identity_principal_id = tostring(p["entra.agent_identity.principal_id"]),
           blueprint_client_id = tostring(p["entra.agent_blueprint.client_id"])
| order by last_seen desc
""",
    # Also from the app's own spans: the harness outcome of every call.
    "Harness outcomes": f"""
AppDependencies
{_SPAN_ATTRS}
| where isnotempty(tostring(p["harness.status"]))
{{agent_filter}}
| project TimeGenerated, agent = AgentName, model = ModelName,
          backend = tostring(p["harness.backend"]),
          status = tostring(p["harness.status"]),
          guardrail_violations = tostring(p["harness.guardrail.violations"]),
          total_tokens = toint(p["gen_ai.usage.total_tokens"]),
          DurationMs, OperationId
| order by TimeGenerated desc
""",
}


def supports_agent_filter(name: str) -> bool:
    return AGENT_FILTER in QUERIES[name]


def build(name: str, agent: str | None = None) -> str:
    """Resolve a named query, optionally scoped to one agent.

    Matching is a prefix so a base name like `news-agent` covers that agent's
    per-model variants. The name is pattern-checked before it reaches the KQL.
    """
    if agent and not _AGENT_NAME.match(agent):
        raise ValueError(f"Invalid agent name: {agent!r}")
    clause = f'| where AgentName startswith "{agent}"' if agent else ""
    return QUERIES[name].replace(AGENT_FILTER, clause)


def is_configured() -> bool:
    return bool(settings.log_analytics_workspace_id)


def query(kql: str, hours: int = 4) -> pd.DataFrame:
    """Run a KQL query against the configured workspace."""
    if not settings.log_analytics_workspace_id:
        raise RuntimeError(
            "LOG_ANALYTICS_WORKSPACE_ID is not set - add it to your .env to query logs."
        )

    client = LogsQueryClient(build_credential())
    response = client.query_workspace(
        workspace_id=settings.log_analytics_workspace_id,
        query=kql,
        timespan=timedelta(hours=hours),
    )

    if response.status == LogsQueryStatus.FAILURE:
        raise RuntimeError(f"Log Analytics query failed: {response.partial_error}")

    tables = getattr(response, "tables", None) or getattr(response, "partial_data", [])
    if not tables:
        return pd.DataFrame()

    table = tables[0]
    return pd.DataFrame(data=table.rows, columns=table.columns)
