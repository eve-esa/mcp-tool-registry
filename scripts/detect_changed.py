"""
Detect which MCP server folders changed in the current push.

Compares HEAD against the previous commit and returns server folder names
whose files were modified.

Usage:
    python scripts/detect_changed.py [--base-ref HEAD~1]

Outputs JSON to stdout:  ["effis", "ticket-manager"]
"""

import json
import subprocess
import sys
from pathlib import Path


def get_changed_files(base_ref: str = "HEAD~1") -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", base_ref, "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [f for f in result.stdout.strip().split("\n") if f]


def extract_server_names(changed_files: list[str]) -> list[str]:
    servers = set()
    for filepath in changed_files:
        parts = Path(filepath).parts
        if len(parts) >= 2 and parts[0] == "servers":
            server_name = parts[1]
            server_dir = Path("servers") / server_name
            if (server_dir / "server.py").exists():
                servers.add(server_name)
    return sorted(servers)


def main():
    base_ref = "HEAD~1"
    if len(sys.argv) > 2 and sys.argv[1] == "--base-ref":
        base_ref = sys.argv[2]

    changed_files = get_changed_files(base_ref)
    servers = extract_server_names(changed_files)

    print(json.dumps(servers))


if __name__ == "__main__":
    main()
