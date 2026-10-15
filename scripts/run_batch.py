"""Send a batch of prompts to one agent through the harness (guardrails and
the spec's batch token budget apply)."""

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

from agent_harness import telemetry
from agent_harness.agents import AGENT_SPECS, AGENTS_BY_KEY
from agent_harness.batch import load_prompts, run_batch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, choices=[s.key for s in AGENT_SPECS])
    parser.add_argument("--dataset", help="Defaults to the spec's evaluation.dataset")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", help="Write the batch summary JSON here")
    args = parser.parse_args()

    dataset = args.dataset or AGENTS_BY_KEY[args.agent].evaluation.dataset
    prompts = load_prompts(dataset, agent_key=args.agent)[: args.limit]
    print(f"sending {len(prompts)} prompts to {args.agent} with {args.workers} workers")

    summary = run_batch(
        args.agent,
        prompts,
        max_workers=args.workers,
        on_progress=lambda done, total: print(f"  {done}/{total}"),
    )

    print(f"batch {summary.batch_id} trace {summary.trace_id}")
    print(f"  succeeded: {summary.succeeded}  failed: {summary.failed}  flagged: {summary.flagged}")
    print(
        f"  tokens in/out/total: {summary.input_tokens}/"
        f"{summary.output_tokens}/{summary.total_tokens}"
    )
    print(f"  avg duration: {summary.avg_duration_ms:.0f} ms")

    for result in summary.results:
        if not result.succeeded:
            print(f"  {result.status.upper()} [{result.agent_name}] {result.error}")
        for violation in result.guardrail_violations:
            print(f"  guardrail [{result.status}] {violation}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "batch_id": summary.batch_id,
            "trace_id": summary.trace_id,
            "agent": summary.agent_key,
            "total": summary.total,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "flagged": summary.flagged,
            "input_tokens": summary.input_tokens,
            "output_tokens": summary.output_tokens,
            "total_tokens": summary.total_tokens,
            "avg_duration_ms": summary.avg_duration_ms,
            "results": [r.to_dict() for r in summary.results],
        }
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        print(f"summary written to {args.out}")

    telemetry.flush()
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
