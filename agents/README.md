# Agent specs

Each `*.yaml` file in this folder defines one agent. The harness loads all of
them at startup, and the Streamlit app and scripts pick them up automatically.
The app creates each agent in Foundry the first time it loads (once; it never
adds a version to an existing agent). To add an agent, add a file here; no code
changes are needed.

Set `HARNESS_AGENTS_DIR` to load specs from a different folder.

## Schema

```yaml
key: news                     # required. lowercase letters, digits, _. Used in CLI flags
name: news-agent              # required. Base agent name; the model is appended (news-agent-gpt-5-4-mini)
name_env: NEWS_AGENT_NAME     # optional. Env var that overrides `name`
label: News                   # optional. Shown in the UI (defaults to key)
description: ...              # optional. Shown in the UI
placeholder: ...              # optional. Chat input placeholder
backend: foundry_prompt       # optional. foundry_prompt (default) | echo | agent_framework | any registered backend
backend_options: {}           # optional. Backend-specific settings (echo: reply: "template {prompt}")

instructions: |               # required. The system instructions
  ...

limits:                       # all optional
  timeout_seconds: 60         # per call, applied by the SDK
  max_retries: 2              # SDK retries on 408/429/5xx with backoff
  session_token_budget: 20000 # per chat session per agent (omit = unlimited)
  batch_token_budget: 50000   # per batch run (omit = unlimited)

guardrails:                   # all optional
  input:                      # runs before the backend is called
    action: block             # block (default for input) | flag
    max_chars: 2000
    blocked_patterns: ['(?i)ignore (all )?previous instructions']
  output:                     # runs on the reply
    action: flag              # flag (default for output) | block
    max_chars: 600
    required_prefix: "..."
    blocked_patterns: []
  hooks:                      # custom checks, run in both stages
    - agent_harness.example_hooks:no_email_addresses

evaluation:                   # all optional
  kinds: [quality, safety]    # evaluator sets the app's Evaluations tab offers (omit = all)
  limit: 5                    # dataset rows per run
  fail_under: 0.6             # pass rate (0-1) the app's Evaluations tab compares the result with
  dataset: ../data/eval_dataset.jsonl   # relative to this file; default shown
```

Unknown fields are rejected, so a typo fails validation instead of being
ignored.

## Built-in backends

- `foundry_prompt` (default): a Foundry-managed prompt agent (versioned, Entra
  Agent ID, server-side evaluations).
- `echo`: a deterministic offline stand-in, for tests and dry runs.
- `agent_framework`: a [Microsoft Agent Framework](https://learn.microsoft.com/agent-framework/overview/agent-framework-overview)
  agent run in this same process, calling a Foundry model deployment
  directly. Foundry never sees an agent resource for this backend - only the
  inference calls it makes. No extra configuration beyond
  `AZURE_AI_PROJECT_ENDPOINT` is required. See
  [examples/agent_framework_demo.yaml](examples/agent_framework_demo.yaml)
  for a spec to copy into this folder.

## What each call goes through

```
input guardrails ─► token budget ─► backend (timeout, retries) ─► output guardrails ─► telemetry
```

| Status | Meaning |
|---|---|
| `ok` | Passed every check |
| `flagged` | An output check in `flag` mode found something; the reply is still returned |
| `blocked` | A check in `block` mode found something. Input blocks never reach the backend; output blocks withhold the reply |
| `budget_exceeded` | The session or batch token budget was already used up, so the call was skipped |
| `error` | The backend call failed (after retries) |

A custom hook that raises an exception counts as a violation (fails closed).

## Validate

```powershell
.venv\Scripts\python.exe scripts\validate_agents.py
```
