"""Streamlit client for the Foundry agents.

On startup this app creates any agent that doesn't exist yet (once, never a new
version), then lets you chat, run batches, run evaluations and query telemetry.
"""

import sys
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_harness import logs  # noqa: E402
from agent_harness.agents import (  # noqa: E402
    AGENT_SPECS,
    AGENTS_BY_KEY,
    agent_name,
    available_models,
    backend_for,
    ensure_provisioned,
)
from agent_harness.batch import load_prompts, run_batch  # noqa: E402
from agent_harness.client import get_clients  # noqa: E402
from agent_harness.config import settings  # noqa: E402
from agent_harness.evaluation import load_queries, pass_rate, run_evaluation  # noqa: E402
from agent_harness.identity import get_identity  # noqa: E402
from agent_harness.runtime import chat_turn, session_budget, start_conversation  # noqa: E402
from agent_harness.specs import EVAL_KINDS  # noqa: E402

st.set_page_config(page_title="Foundry Agent Test Harness", page_icon="📡", layout="wide")


@st.cache_resource(show_spinner="Connecting to Foundry…")
def bootstrap():
    return get_clients()


@st.cache_resource(show_spinner="Checking agents…")
def agent_versions(model: str) -> dict[str, str]:
    """Cached per model, so switching only re-versions an agent the first time."""
    return ensure_provisioned(model)


@st.cache_data(ttl=300, show_spinner=False)
def model_choices() -> list[str]:
    return available_models()


@st.cache_data(ttl=300, show_spinner=False)
def entra_identity(name: str) -> dict | None:
    info = get_identity(name)
    return info.to_dict() if info else None


if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
    st.session_state.history = {spec.key: [] for spec in AGENT_SPECS}
    st.session_state.conversations = {}
    st.session_state.budgets = {}


def chat_budget(key: str):
    """One token budget per agent for this browser session; Clear chat resets it."""
    if key not in st.session_state.budgets:
        st.session_state.budgets[key] = session_budget(key)
    return st.session_state.budgets[key]


st.title("📡 Foundry Agent Test Harness")

with st.sidebar:
    try:
        clients = bootstrap()
    except Exception as exc:
        st.error(f"Connection failed: {exc}")
        st.stop()

    with st.expander("🔌 Integrations"):
        st.markdown("**Foundry Project**")
        st.caption(settings.project_endpoint)

    models = model_choices()
    model = st.selectbox(
        "Model",
        models,
        index=models.index(settings.model_deployment)
        if settings.model_deployment in models
        else 0,
    )

    st.divider()
    st.subheader("Agent")

    try:
        versions = agent_versions(model)
    except Exception as exc:
        st.error(f"Agent provisioning failed: {exc}")
        st.stop()

    agent_key = st.radio(
        "Choose an agent",
        [spec.key for spec in AGENT_SPECS],
        format_func=lambda k: AGENTS_BY_KEY[k].label,
        label_visibility="collapsed",
    )
    spec = AGENTS_BY_KEY[agent_key]
    version = versions.get(agent_key)

    st.caption(spec.description)
    if version:
        st.caption(f"`{spec.name}` · `{model}` · v{version} · backend `{spec.backend}`")
        st.caption(f"Agent: `{agent_name(spec, model)}`")
    else:
        st.error(f"`{agent_name(spec, model)}` has no versions.")

    with st.expander("Harness limits and guardrails"):
        budget = chat_budget(agent_key)
        st.caption(
            f"Timeout {spec.limits.timeout_seconds:g}s · retries {spec.limits.max_retries}  \n"
            f"Session tokens {budget.used}"
            + (f" / {budget.limit}" if budget.limit else " (no limit)")
        )
        st.caption(
            f"Input guardrail: {spec.guardrails.input.action} · "
            f"{len(spec.guardrails.input.blocked_patterns)} pattern(s)"
            + (f" · max {spec.guardrails.input.max_chars} chars" if spec.guardrails.input.max_chars else "")
        )
        st.caption(
            f"Output guardrail: {spec.guardrails.output.action}"
            + (" · required prefix" if spec.guardrails.output.required_prefix else "")
            + f" · {len(spec.guardrails.output.blocked_patterns)} pattern(s)"
            + (f" · {len(spec.guardrails.hooks)} custom hook(s)" if spec.guardrails.hooks else "")
        )

    if version and backend_for(spec).supports_agent_identity:
        st.divider()
        st.subheader("Entra Agent ID")
        try:
            ident = entra_identity(agent_name(spec, model))
        except Exception as exc:
            st.warning(f"Couldn't read the agent identity: {exc}")
            ident = None
        if ident and ident["is_legacy"]:
            st.warning(
                "Legacy agent: runs as the project's shared agent identity. "
                "Run `scripts/agent_identities.py --recreate-legacy --yes` "
                "to give it a distinct Entra Agent ID."
            )
        elif ident:
            instance, blueprint = ident["instance"], ident["blueprint"]
            st.caption(
                "Agent identity"
                + (f" · {instance['status']}" if instance.get("status") else "")
                + f"  \nclient `{instance['client_id']}`"
                + f"  \nprincipal `{instance['principal_id']}`"
            )
            if blueprint:
                st.caption(
                    "Blueprint"
                    + (f" · {blueprint['status']}" if blueprint.get("status") else "")
                    + f"  \nclient `{blueprint['client_id']}`"
                )
            if ident.get("state") and ident["state"] != "enabled":
                st.error(
                    f"Agent is {ident['state']} "
                    f"(source: {ident.get('state_source') or 'n/a'})"
                )

    st.divider()
    st.caption(f"Session `{st.session_state.session_id[:8]}`")
    if st.button("Clear chat", use_container_width=True):
        st.session_state.history[agent_key] = []
        st.session_state.conversations.pop(agent_key, None)
        st.session_state.budgets.pop(agent_key, None)
        st.rerun()


chat_tab, batch_tab, eval_tab, telemetry_tab = st.tabs(
    ["Chat", "Batch", "Evaluations", "Telemetry"]
)


with chat_tab:
    st.subheader(f"Chat with the {spec.label} agent")

    for turn in st.session_state.history[agent_key]:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn.get("meta"):
                st.caption(turn["meta"])

    prompt = st.chat_input(spec.placeholder, disabled=version is None)

    if prompt:
        st.session_state.history[agent_key].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                # Reuse the server-side conversation so the agent keeps context.
                conversation_id = st.session_state.conversations.get(agent_key)
                if conversation_id is None:
                    conversation_id = start_conversation(agent_key)
                    st.session_state.conversations[agent_key] = conversation_id

                result = chat_turn(
                    agent_key=agent_key,
                    message=prompt,
                    conversation_id=conversation_id,
                    session_id=st.session_state.session_id,
                    model=model,
                    budget=chat_budget(agent_key),
                )

            if result.status == "error":
                st.error(result.error)
                meta = "failed"
            elif result.status in ("blocked", "budget_exceeded"):
                st.warning(result.error)
                meta = result.status.replace("_", " ")
            else:
                st.markdown(result.output_text)
                meta = (
                    f"{result.total_tokens} tokens "
                    f"(in {result.input_tokens} / out {result.output_tokens}) · "
                    f"{result.duration_ms:.0f} ms · trace `{result.trace_id}`"
                )
                st.caption(meta)
            for violation in result.guardrail_violations:
                st.caption(f"🛡️ {violation}")
                meta += f"  \n🛡️ {violation}"

            st.session_state.history[agent_key].append(
                {
                    "role": "assistant",
                    "content": result.output_text or f"⚠️ {result.error}",
                    "meta": meta,
                }
            )


with batch_tab:
    st.subheader(f"Send a batch of prompts to the {spec.label} agent")
    col_a, col_b = st.columns(2)
    limit = col_a.slider("Prompts", 1, 12, 6)
    workers = col_b.slider("Concurrency", 1, 8, 4)

    if st.button("Run batch", type="primary", disabled=version is None):
        prompts = load_prompts(spec.evaluation.dataset, agent_key=agent_key)[:limit]
        progress = st.progress(0.0, text="Starting…")

        def on_progress(done: int, total: int):
            progress.progress(done / total, text=f"{done}/{total} complete")

        with st.spinner("Running batch…"):
            summary = run_batch(
                agent_key, prompts, max_workers=workers, on_progress=on_progress, model=model
            )
        progress.empty()

        cols = st.columns(5)
        cols[0].metric("Succeeded", summary.succeeded)
        cols[1].metric("Flagged", summary.flagged)
        cols[2].metric("Failed", summary.failed)
        cols[3].metric("Total tokens", summary.total_tokens)
        cols[4].metric("Avg duration", f"{summary.avg_duration_ms:.0f} ms")

        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "prompt": r.prompt[:60],
                        "tokens": r.total_tokens,
                        "ms": round(r.duration_ms),
                        "status": r.status,
                        "guardrails": "; ".join(r.guardrail_violations),
                        "trace_id": r.trace_id,
                    }
                    for r in summary.results
                ]
            ),
            use_container_width=True,
        )


with eval_tab:
    st.subheader(f"Evaluate the {spec.label} agent")
    can_evaluate_server_side = backend_for(spec).supports_foundry_evaluation
    st.caption(
        "Runs server-side via Foundry's evaluation service."
        if can_evaluate_server_side
        else f"Backend `{spec.backend}` has no Foundry agent resource to target, "
        "so this runs locally with the Azure AI Evaluation SDK instead - same "
        "evaluator types, computed in this process."
    )
    col_a, col_b = st.columns(2)
    kind = col_a.selectbox("Evaluators", list(spec.evaluation.kinds or EVAL_KINDS))
    limit = col_b.slider("Items", 1, 12, spec.evaluation.limit, key="eval_limit")

    st.caption(
        "quality = fluency, coherence, task adherence · "
        "safety = violence, hate/unfairness, self-harm"
    )

    if st.button("Run evaluation", type="primary", disabled=version is None):
        queries = load_queries(spec.evaluation.dataset, limit=limit, agent_key=agent_key)
        status_box = st.empty()

        spinner_text = (
            "Evaluation running server-side…"
            if can_evaluate_server_side
            else "Evaluation running locally…"
        )
        with st.spinner(spinner_text):
            result = run_evaluation(
                agent_key=agent_key,
                queries=queries,
                kind=kind,
                model=model,
                on_status=lambda s: status_box.info(f"Status: {s}"),
            )
        status_box.empty()

        if result.status == "completed":
            rate = pass_rate(result)
            threshold = spec.evaluation.fail_under
            summary = f"Completed · run `{result.run_id}` · pass rate {rate:.0%}"
            if threshold is None:
                st.success(summary)
            elif rate >= threshold:
                st.success(f"{summary} (threshold {threshold:.0%})")
            else:
                st.error(f"{summary} is below the {threshold:.0%} threshold for this agent")
        else:
            st.error(f"Run ended with status: {result.status}")

        st.write("Result counts", result.result_counts)

        rows = []
        for item in result.items:
            row = {"query": (item["query"] or "")[:60]}
            for name, score in item["scores"].items():
                row[name] = score.get("score")
            rows.append(row)
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
            with st.expander("Evaluator reasoning"):
                for item in result.items:
                    st.markdown(f"**{item['query']}**")
                    for name, score in item["scores"].items():
                        if score.get("reason"):
                            st.caption(f"_{name}_: {score['reason']}")


with telemetry_tab:
    st.subheader("Log Analytics")

    if not logs.is_configured():
        st.info(
            "Set `LOG_ANALYTICS_WORKSPACE_ID` in your .env to query telemetry here. "
            "Traces are still being exported to Application Insights."
        )
    else:
        col_a, col_b, col_c = st.columns([3, 2, 1])
        name = col_a.selectbox("Query", list(logs.QUERIES))

        agent_options = ["All agents"] + [s.name for s in AGENT_SPECS]
        selected_agent = col_b.selectbox("Agent", agent_options)
        hours = col_c.number_input("Hours", 1, 168, 4)

        agent = None if selected_agent == "All agents" else selected_agent
        if agent and not logs.supports_agent_filter(name):
            st.caption("This query isn't agent-scoped, so the filter is ignored.")

        kql = logs.build(name, agent)
        st.code(kql.strip(), language="kusto")

        if st.button("Run query", type="primary"):
            with st.spinner("Querying workspace…"):
                try:
                    frame = logs.query(kql, hours=int(hours))
                except Exception as exc:
                    st.error(str(exc))
                else:
                    if frame.empty:
                        st.warning("No rows returned.")
                    else:
                        st.dataframe(frame, use_container_width=True)
