"""Validate the agent specs offline - no Azure calls.

Checks every YAML spec parses, uses a registered backend, has importable
guardrail hooks, only asks for evaluations its backend supports, and has
evaluation data.
"""

import argparse

import _bootstrap  # noqa: F401

from agent_harness.backends import backend_names, get_backend
from agent_harness.datasets import load_prompts
from agent_harness.guardrails import load_hook
from agent_harness.specs import SpecError, load_specs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", help="Agents directory (defaults to HARNESS_AGENTS_DIR or ./agents)")
    args = parser.parse_args()

    try:
        specs = load_specs(args.dir)
    except (SpecError, OSError) as exc:
        print(f"FAIL {exc}")
        return 1

    errors: list[str] = []
    for spec in specs:
        where = spec.source.name
        try:
            backend = get_backend(spec.backend)
        except KeyError:
            errors.append(f"{where}: backend {spec.backend!r} not in {backend_names()}")
            continue

        for hook in spec.guardrails.hooks:
            try:
                load_hook(hook)
            except Exception as exc:
                errors.append(f"{where}: guardrail hook {hook!r} can't be loaded: {exc}")

        if spec.evaluation.kinds:
            if not (backend.supports_foundry_evaluation or backend.supports_local_evaluation):
                errors.append(
                    f"{where}: evaluation.kinds is set but backend {spec.backend!r} "
                    "can't be evaluated"
                )
            if not spec.evaluation.dataset.exists():
                errors.append(f"{where}: dataset {spec.evaluation.dataset} not found")
            elif not load_prompts(spec.evaluation.dataset, agent_key=spec.key):
                errors.append(f"{where}: dataset has no rows for agent {spec.key!r}")

        print(
            f"ok  {spec.key:<12} {spec.name:<20} backend={spec.backend} "
            f"evals={','.join(spec.evaluation.kinds) or '-'} "
            f"hooks={len(spec.guardrails.hooks)}"
        )

    for error in errors:
        print(f"FAIL {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
