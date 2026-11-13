"""Load a server module, attach output-file middleware, and run it."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from fastmcp import FastMCP

from eve_mcp_runtime.files import OutputFilesMiddleware


def load_server(path: Path) -> FastMCP:
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("server", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"{path}: cannot load")
    module = importlib.util.module_from_spec(spec)
    sys.modules["server"] = module
    spec.loader.exec_module(module)
    servers = [value for value in vars(module).values() if isinstance(value, FastMCP)]
    if len(servers) != 1:
        raise SystemExit(f"{path}: expected one FastMCP instance, found {len(servers)}")
    return servers[0]


def main() -> None:
    parser = argparse.ArgumentParser(prog="eve-mcp")
    parser.add_argument("server", type=Path)
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Load the server, report that middleware would attach, and exit",
    )
    args = parser.parse_args()

    path = args.server.resolve()
    server = load_server(path)
    server.add_middleware(OutputFilesMiddleware(server_dir=path.parent))
    if args.check:
        print(f"fastmcp {path}")
        return
    if args.transport == "stdio":
        server.run(transport="stdio")
        return
    server.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=args.port,
        path="/mcp",
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
