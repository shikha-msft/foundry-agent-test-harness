"""Run an AI Red Teaming scan against one of the demo agents."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import _bootstrap  # noqa: F401

from agent_harness.agents import AGENT_SPECS
from agent_harness.redteam import DEFAULT_STRATEGIES, run_redteam


def attack_success_rate(result) -> float:
    """Fraction of scored attacks where an evaluator did NOT pass."""
    checks = [
        score.get("passed")
        for item in result.items
        for score in item["scores"].values()
        if score.get("passed") is not None
    ]
    if not checks:
        return 0.0
    return sum(1 for c in checks if not c) / len(checks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, choices=[s.key for s in AGENT_SPECS])
    parser.add_argument("--strategies", nargs="*", default=DEFAULT_STRATEGIES)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--out", help="Write the result JSON here")
    parser.add_argument(
        "--fail-over",
        type=float,
        default=None,
        help="Exit non-zero if attack success rate exceeds this (0-1).",
    )
    args = parser.parse_args()

    print(f"red teaming agent={args.agent} strategies={args.strategies} turns={args.turns}")

    result = run_redteam(
        agent_key=args.agent,
        strategies=args.strategies,
        num_turns=args.turns,
        on_status=lambda s: print(f"  status: {s}"),
    )

    print(f"run {result.run_id} finished with status={result.status}")
    print(f"result counts: {result.result_counts}")

    asr = attack_success_rate(result)
    print(f"attack success rate: {asr:.1%}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({**asdict(result), "attack_success_rate": asr}, handle, indent=2)
        print(f"results written to {args.out}")

    if result.status != "completed":
        print("red team run did not complete")
        return 1

    if args.fail_over is not None and asr > args.fail_over:
        print(f"FAIL: attack success rate {asr:.1%} exceeds threshold {args.fail_over:.1%}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
