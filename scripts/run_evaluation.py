"""Run a Foundry evaluation from the command line.

The app's Evaluations tab does the same thing; this is for scripting. Defaults
(dataset, item limit, pass-rate threshold) come from the agent's spec;
command-line flags override them. Exits 1 if the pass rate is below the threshold.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import _bootstrap  # noqa: F401

from agent_harness.agents import AGENT_SPECS, AGENTS_BY_KEY
from agent_harness.evaluation import load_queries, pass_rate, run_evaluation
from agent_harness.specs import EVAL_KINDS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, choices=[s.key for s in AGENT_SPECS])
    parser.add_argument("--kind", default="quality", choices=EVAL_KINDS)
    parser.add_argument("--dataset", help="Defaults to the spec's evaluation.dataset")
    parser.add_argument("--limit", type=int, help="Defaults to the spec's evaluation.limit")
    parser.add_argument("--out", help="Write the result JSON here")
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="Exit non-zero if the evaluator pass rate falls below this (0-1). "
        "Defaults to the spec's evaluation.fail_under.",
    )
    args = parser.parse_args()

    spec = AGENTS_BY_KEY[args.agent]
    dataset = args.dataset or spec.evaluation.dataset
    limit = args.limit or spec.evaluation.limit
    fail_under = args.fail_under if args.fail_under is not None else spec.evaluation.fail_under

    queries = load_queries(dataset, limit=limit, agent_key=args.agent)
    if not queries:
        print(f"no queries for agent {args.agent!r} in {dataset}")
        return 1
    print(f"evaluating agent={args.agent} kind={args.kind} items={len(queries)}")

    result = run_evaluation(
        agent_key=args.agent,
        queries=queries,
        kind=args.kind,
        on_status=lambda s: print(f"  status: {s}"),
    )

    print(f"run {result.run_id} finished with status={result.status}")
    print(f"result counts: {result.result_counts}")

    rate = pass_rate(result)
    print(f"evaluator pass rate: {rate:.1%}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({**asdict(result), "pass_rate": rate}, handle, indent=2)
        print(f"results written to {args.out}")

    if result.status != "completed":
        print("evaluation did not complete")
        return 1

    if fail_under is not None and rate < fail_under:
        print(f"FAIL: pass rate {rate:.1%} is below threshold {fail_under:.1%}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
