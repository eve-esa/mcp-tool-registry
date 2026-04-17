"""
Health-check all deployed MCP servers on AgentCore.

Discovers every runtime via the AgentCore API, obtains a Cognito token,
connects over MCP, lists tools, and optionally calls a smoke-test tool
on each server.  Prints a summary table at the end.

Usage:
    python scripts/check_servers.py
    python scripts/check_servers.py --region eu-west-1 --skip-call
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import boto3
import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SMOKE_TESTS: dict[str, dict] = {
    "eve_retrieval": {
        "tool": "retrieve",
        "args": {"query": "What is ESA?", "k": 1},
    },
    "effis": {
        "tool": "geocode_place",
        "args": {"place_name": "Rome", "limit": 1},
    },
}


def _discover_runtimes(region: str) -> list[dict]:
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    runtimes = []
    token = None
    while True:
        kwargs = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_agent_runtimes(**kwargs)
        for rt in resp.get("agentRuntimes", []):
            detail = client.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
            runtimes.append(detail)
        token = resp.get("nextToken")
        if not token:
            break
    return runtimes


def _get_cognito_token(discovery_url: str, client_id: str) -> str | None:
    """Fetch the OIDC config, then request a client_credentials token."""
    try:
        oidc = requests.get(discovery_url, timeout=10).json()
        token_url = oidc["token_endpoint"]
    except Exception as exc:
        print(f"    Could not fetch OIDC config: {exc}")
        return None

    pool_clients = _list_pool_clients_for_discovery(discovery_url)

    for pc_id, pc_secret in pool_clients:
        if pc_id not in client_id.split(",") if "," in client_id else [client_id]:
            if pc_id != client_id:
                continue
        try:
            resp = requests.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": pc_id,
                    "client_secret": pc_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()["access_token"]
        except Exception:
            continue

    return None


def _list_pool_clients_for_discovery(
    discovery_url: str,
) -> list[tuple[str, str]]:
    """Extract user-pool ID from the discovery URL and list app clients."""
    import re

    match = re.search(r"/([a-z0-9-]+_[A-Za-z0-9]+)/", discovery_url)
    if not match:
        return []
    pool_id = match.group(1)
    region = pool_id.split("_")[0]

    cognito = boto3.client("cognito-idp", region_name=region)
    clients_resp = cognito.list_user_pool_clients(
        UserPoolId=pool_id, MaxResults=20
    )
    results = []
    for c in clients_resp.get("UserPoolClients", []):
        detail = cognito.describe_user_pool_client(
            UserPoolId=pool_id, ClientId=c["ClientId"]
        )
        uc = detail["UserPoolClient"]
        if "client_credentials" in uc.get("AllowedOAuthFlows", []):
            results.append((uc["ClientId"], uc.get("ClientSecret", "")))
    return results


def _build_mcp_url(runtime: dict, region: str) -> str:
    arn = runtime["agentRuntimeArn"]
    from urllib.parse import quote

    encoded_arn = quote(arn, safe="")
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )


async def _check_one(
    runtime: dict,
    region: str,
    token: str,
    skip_call: bool,
) -> dict:
    name = runtime["agentRuntimeName"]
    result = {"name": name, "status": runtime["status"], "tools": [], "smoke": None}
    mcp_url = _build_mcp_url(runtime, region)

    try:
        headers = {"Authorization": f"Bearer {token}"}
        async with streamablehttp_client(mcp_url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                result["tools"] = [t.name for t in tools_resp.tools]

                if skip_call or name not in SMOKE_TESTS:
                    result["smoke"] = "skipped"
                else:
                    spec = SMOKE_TESTS[name]
                    call_result = await session.call_tool(spec["tool"], spec["args"])
                    text = call_result.content[0].text
                    parsed = json.loads(text)
                    if "error" in parsed:
                        result["smoke"] = f"FAIL: {parsed['error']}"
                    else:
                        result["smoke"] = "OK"
    except Exception as exc:
        result["smoke"] = f"FAIL: {exc}"

    return result


async def main(region: str, skip_call: bool) -> int:
    print(f"Discovering runtimes in {region}...\n")
    runtimes = _discover_runtimes(region)

    if not runtimes:
        print("No AgentCore runtimes found.")
        return 1

    print(f"Found {len(runtimes)} runtime(s):\n")

    results = []
    for rt in runtimes:
        name = rt["agentRuntimeName"]
        status = rt["status"]
        print(f"  [{name}] status={status}")

        if status != "READY":
            results.append({"name": name, "status": status, "tools": [], "smoke": "NOT READY"})
            continue

        auth = rt.get("authorizerConfiguration", {})
        jwt_auth = auth.get("customJWTAuthorizer", {})
        discovery_url = jwt_auth.get("discoveryUrl", "")
        allowed = jwt_auth.get("allowedClients", [])

        if not discovery_url or not allowed:
            results.append({"name": name, "status": status, "tools": [], "smoke": "NO AUTH CONFIG"})
            continue

        print(f"    Obtaining token (allowedClients={allowed})...")
        token = _get_cognito_token(discovery_url, allowed[0])
        if not token:
            results.append({"name": name, "status": status, "tools": [], "smoke": "TOKEN FAILED"})
            continue

        t0 = time.time()
        print(f"    Connecting via MCP...")
        res = await _check_one(rt, region, token, skip_call)
        elapsed = time.time() - t0
        res["time"] = f"{elapsed:.1f}s"
        results.append(res)
        print(f"    Tools: {res['tools']}  |  Smoke: {res['smoke']}  |  {res['time']}")

    print("\n" + "=" * 70)
    print(f"{'Server':<20} {'Status':<10} {'Tools':<30} {'Smoke':<15} {'Time'}")
    print("-" * 70)
    for r in results:
        tools_str = ", ".join(r["tools"]) if r["tools"] else "—"
        print(f"{r['name']:<20} {r['status']:<10} {tools_str:<30} {r.get('smoke','—'):<15} {r.get('time','—')}")
    print("=" * 70)

    failures = [r for r in results if r.get("smoke") not in ("OK", "skipped")]
    if failures:
        print(f"\n{len(failures)} server(s) FAILED.")
        return 1
    print(f"\nAll {len(results)} server(s) healthy.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Health-check all deployed MCP servers")
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--skip-call", action="store_true", help="Only list tools, skip smoke-test calls")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.region, args.skip_call)))
