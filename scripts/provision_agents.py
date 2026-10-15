"""Create or update the demo agents in Foundry by hand: adds a new version of
every agent on each run. The app creates agents on its own (once); use this
only to publish changed instructions on purpose.

The manifest records each agent's Entra Agent ID.
"""

import argparse
import json

import _bootstrap  # noqa: F401

from agent_harness.agents import provision_all
from agent_harness.backends import get_backend
from agent_harness.identity import get_identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="Write the provisioned agent manifest to this path")
    args = parser.parse_args()

    agents = provision_all()

    for agent in agents:
        if not get_backend(agent["backend"]).supports_agent_identity:
            agent["entra_agent_id"] = None
            print(f"provisioned {agent['name']} v{agent['version']} ({agent['backend']})")
            continue
        info = get_identity(agent["name"])
        agent["entra_agent_id"] = info.to_dict() if info else None
        if info is None or info.is_legacy:
            who = "shared project identity (legacy agent)"
        else:
            who = f"agent identity {info.instance.client_id}"
        print(f"provisioned {agent['name']} v{agent['version']} ({agent['model']}) - {who}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(agents, handle, indent=2)
        print(f"manifest written to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
