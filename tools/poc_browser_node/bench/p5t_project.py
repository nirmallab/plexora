"""P5T's desktop arm: a Plexora data root whose one project reads its image from a data node.

    python bench/p5t_project.py --data DATA_ROOT --node NAME --endpoint URL --image PATH_ON_NODE \
        [--name nlu337] [--token-env POC_NODE_TOKEN]

Registers the node in DATA_ROOT (the bench's scratch root, never the user's)
with the browser going to it directly (`browser_endpoint` = `endpoint`), then
makes the project with plexora.datasets.create_project("node://NAME/PATH"),
as a script over a cluster's files would. The token is read from the
environment variable `--token-env` names, never from the command line. Prints
{name, channels} on its last line.
"""

from __future__ import annotations

import argparse
import json
import os


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--name", default="nlu337")
    parser.add_argument("--token-env", default="POC_NODE_TOKEN")
    args = parser.parse_args(argv)
    os.environ["PLEXORA_DATA_PATH"] = args.data
    os.environ.setdefault("PLEXORA_TELEMETRY", "off")
    from plexora.datasets import create_project
    from plexora.nodes import register_node

    register_node(args.node, args.endpoint, token=os.environ[args.token_env],
                  browser_endpoint=args.endpoint)
    if not os.path.isdir(os.path.join(args.data, args.name)):
        create_project(f"node://{args.node}/{args.image}", name=args.name)
    from plexora.server.models.project import Project  # noqa: F401  (import check only)
    config = json.load(open(os.path.join(args.data, "config.json"), encoding="utf-8"))
    entry = config.get(args.name, {})
    print(json.dumps({"name": args.name, "channels": len(entry.get("imageData") or []),
                      "resources": entry.get("resources")}))


if __name__ == "__main__":
    main()
