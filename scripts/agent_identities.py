"""Show, audit and fix the Entra Agent ID of the demo agents.

  show (default)       blueprint + agent identity per agent
  --audit              also list each agent identity's Azure RBAC roles
  --recreate-legacy    delete and recreate legacy agents so they get a
                       distinct agent identity (destructive, needs --yes)
  --fail-on-legacy     exit 1 if any agent still uses the shared project identity
  --fail-on-privileged exit 1 if any agent identity holds a privileged role
"""

import argparse
import json
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401

from agent_harness.agents import AGENT_SPECS, agent_name, backend_for
from agent_harness.config import settings
from agent_harness.identity import get_identity, recreate_legacy, role_assignments


def _status(identity) -> str:
    return f" ({identity.status})" if identity.status else ""


def _describe(info) -> list[str]:
    if info.is_legacy:
        return ["  identity: shared project identity (legacy agent - no distinct Entra Agent ID)"]
    lines = [
        f"  agent identity: client {info.instance.client_id} "
        f"principal {info.instance.principal_id}{_status(info.instance)}",
    ]
    if info.blueprint:
        lines.append(
            f"  blueprint:      client {info.blueprint.client_id} "
            f"principal {info.blueprint.principal_id}{_status(info.blueprint)}"
        )
    if info.state and info.state != "enabled":
        lines.append(f"  state: {info.state} (source: {info.state_source or 'n/a'})")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", default=settings.model_deployment)
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--recreate-legacy", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Confirm --recreate-legacy")
    parser.add_argument("--fail-on-legacy", action="store_true")
    parser.add_argument("--fail-on-privileged", action="store_true")
    parser.add_argument("--out", help="Write the identity report JSON here")
    args = parser.parse_args()

    if args.recreate_legacy and not args.yes:
        parser.error("--recreate-legacy deletes every version of a legacy agent; add --yes to confirm")

    report = []
    legacy, privileged, audit_errors = [], [], []

    for spec in AGENT_SPECS:
        name = agent_name(spec, args.model)
        if not backend_for(spec).supports_agent_identity:
            print(f"{name}: backend {spec.backend!r} has no Entra Agent ID - skipped")
            continue
        info = get_identity(name)
        if info is None:
            print(f"{name}: not found (run provision_agents.py or the app first)")
            continue

        if info.is_legacy and args.recreate_legacy:
            print(f"{name}: legacy agent - recreating to get a distinct agent identity")
            info = recreate_legacy(spec, args.model)

        if args.audit and info.instance:
            try:
                info = replace(info, role_assignments=role_assignments(info.instance.principal_id))
            except RuntimeError as exc:
                audit_errors.append(f"{name}: {exc}")

        print(name)
        for line in _describe(info):
            print(line)
        if args.audit and info.instance:
            if info.role_assignments:
                for assignment in info.role_assignments:
                    flag = "  <-- privileged" if assignment["role"] in info.privileged_roles else ""
                    print(f"  role: {assignment['role']} @ {assignment['scope']}{flag}")
            else:
                print("  roles: none (least privilege for a tool-less prompt agent)")

        if info.is_legacy:
            legacy.append(name)
        if info.privileged_roles:
            privileged.append(f"{name}: {', '.join(info.privileged_roles)}")
        report.append(info.to_dict())

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"report written to {args.out}")

    for error in audit_errors:
        print(f"WARNING could not list role assignments for {error}")

    failed = False
    if args.fail_on_legacy and legacy:
        print(
            "FAIL legacy agents use the shared project identity: "
            + ", ".join(legacy)
            + ". Run scripts/agent_identities.py --recreate-legacy --yes"
        )
        failed = True
    if args.fail_on_privileged and privileged:
        print("FAIL agent identities hold privileged roles: " + "; ".join(privileged))
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
