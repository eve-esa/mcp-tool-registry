"""
Deploy a single MCP server to AgentCore Runtime.

Builds the Docker image, pushes to ECR, then creates or updates
the AgentCore runtime.

Usage:
    python scripts/deploy_server.py \
        --server-name effis \
        --region eu-west-1 \
        --account-id 123456789012 \
        --execution-role-arn arn:aws:iam::123456789012:role/... \
        [--cognito-discovery-url https://...] \
        [--cognito-client-id abc123,def456]
"""

import argparse
import os
import subprocess
from pathlib import Path

import boto3


DEFAULT_HEADER_ALLOWLIST: dict[str, list[str]] = {
    "effis": ["X-CDSE-Client-Id", "X-CDSE-Client-Secret"],
    "serpapi": ["X-API-Key"],
    "eve_retrieval": ["X-EVE-Token", "X-EVE-Email", "X-EVE-Password"],
}

def _resolve_dockerfile(server_name: str) -> tuple[str, str]:
    """Return ``(dockerfile_path, build_context)`` relative to the process cwd.

    If ``servers/<name>/Dockerfile`` exists, use it; otherwise ``shared/Dockerfile``.
    """
    ctx = f"servers/{server_name}"
    custom = Path(ctx) / "Dockerfile"
    if custom.is_file():
        path = str(custom)
        print(f"  Using custom Dockerfile: {path}")
        return path, ctx
    print("  Using shared Dockerfile: shared/Dockerfile")
    return "shared/Dockerfile", ctx


def build_and_push_image(server_name: str, account_id: str, region: str) -> str:
    ecr_repo = f"mcp-servers/{server_name}"
    image_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{ecr_repo}:latest"

    ecr = boto3.client("ecr", region_name=region)
    try:
        ecr.create_repository(
            repositoryName=ecr_repo,
            imageTagMutability="MUTABLE",
            imageScanningConfiguration={"scanOnPush": True},
            encryptionConfiguration={"encryptionType": "AES256"},
        )
        print(f"  Created ECR repo: {ecr_repo}")
    except ecr.exceptions.RepositoryAlreadyExistsException:
        print(f"  ECR repo exists:  {ecr_repo}")

    login_pw = subprocess.run(
        ["aws", "ecr", "get-login-password", "--region", region],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        [
            "docker", "login",
            "--username", "AWS",
            "--password-stdin",
            f"{account_id}.dkr.ecr.{region}.amazonaws.com",
        ],
        input=login_pw.stdout,
        text=True,
        check=True,
    )

    dockerfile, build_context = _resolve_dockerfile(server_name)
    print(f"  Building image for {server_name} (linux/arm64)...")
    subprocess.run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/arm64",
            "-f",
            dockerfile,
            "-t",
            image_uri,
            "--load",
            build_context,
        ],
        check=True,
    )

    print("  Pushing to ECR...")
    subprocess.run(["docker", "push", image_uri], check=True)

    return image_uri


def find_runtime_by_name(client, name: str) -> dict | None:
    """Look up an AgentCore runtime by name. Returns the runtime dict or None."""
    paginator_token = None
    while True:
        kwargs = {"maxResults": 100}
        if paginator_token:
            kwargs["nextToken"] = paginator_token
        response = client.list_agent_runtimes(**kwargs)
        for runtime in response.get("agentRuntimes", []):
            if runtime.get("agentRuntimeName") == name:
                return runtime
        paginator_token = response.get("nextToken")
        if not paginator_token:
            return None


def _build_authorizer_config(
    cognito_discovery_url: str | None,
    cognito_client_id: str | None,
) -> dict | None:
    if cognito_discovery_url and cognito_client_id:
        allowed = [c.strip() for c in cognito_client_id.split(",") if c.strip()]
        return {
            "customJWTAuthorizer": {
                "discoveryUrl": cognito_discovery_url,
                "allowedClients": allowed,
            }
        }
    return None


def _build_header_config(headers: list[str] | None) -> dict | None:
    if not headers:
        return None
    return {
        "requestHeaderAllowlist": headers
    }


def _get_default_headers_for_server(server_name: str) -> list[str] | None:
    """Return default header allowlist for known servers."""
    return DEFAULT_HEADER_ALLOWLIST.get(server_name)


def deploy_to_agentcore(
    server_name: str,
    image_uri: str,
    region: str,
    execution_role_arn: str,
    cognito_discovery_url: str | None = None,
    cognito_client_id: str | None = None,
    header_allowlist: list[str] | None = None,
) -> str:
    client = boto3.client("bedrock-agentcore-control", region_name=region)

    existing = find_runtime_by_name(client, server_name)
    auth_config = _build_authorizer_config(cognito_discovery_url, cognito_client_id)
    header_config = _build_header_config(header_allowlist)

    if existing:
        runtime_id = existing["agentRuntimeId"]
        runtime_arn = existing["agentRuntimeArn"]
        print(f"  Updating existing runtime: {runtime_arn}")

        update_kwargs = {
            "agentRuntimeId": runtime_id,
            "agentRuntimeArtifact": {
                "containerConfiguration": {"containerUri": image_uri}
            },
            "roleArn": execution_role_arn,
            "protocolConfiguration": {"serverProtocol": "MCP"},
            "networkConfiguration": {"networkMode": "PUBLIC"},
        }

        if auth_config:
            update_kwargs["authorizerConfiguration"] = auth_config

        if header_config:
            update_kwargs["requestHeaderConfiguration"] = header_config

        client.update_agent_runtime(**update_kwargs)
        print(f"  Updated:  {runtime_arn}")
        return runtime_arn

    else:
        print(f"  Creating new runtime: {server_name}")

        create_kwargs = {
            "agentRuntimeName": server_name,
            "roleArn": execution_role_arn,
            "agentRuntimeArtifact": {
                "containerConfiguration": {"containerUri": image_uri}
            },
            "protocolConfiguration": {"serverProtocol": "MCP"},
            "networkConfiguration": {"networkMode": "PUBLIC"},
        }

        if auth_config:
            create_kwargs["authorizerConfiguration"] = auth_config

        if header_config:
            create_kwargs["requestHeaderConfiguration"] = header_config

        response = client.create_agent_runtime(**create_kwargs)
        runtime_arn = response["agentRuntimeArn"]
        print(f"  Created:  {runtime_arn}")
        return runtime_arn


def main():
    parser = argparse.ArgumentParser(description="Deploy an MCP server to AgentCore")
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--execution-role-arn", required=True)
    parser.add_argument("--cognito-discovery-url", default=None)
    parser.add_argument("--cognito-client-id", default=None)
    parser.add_argument(
        "--header-allowlist",
        default=None,
        help="Comma-separated list of headers to allow (e.g. 'X-API-Key,X-Custom-Header')",
    )
    args = parser.parse_args()

    header_allowlist = None
    if args.header_allowlist:
        header_allowlist = [h.strip() for h in args.header_allowlist.split(",") if h.strip()]
    else:
        header_allowlist = _get_default_headers_for_server(args.server_name)
        if header_allowlist:
            print(f"  Using default headers for {args.server_name}: {header_allowlist}")

    print(f"\n{'='*60}")
    print(f"  Deploying MCP server: {args.server_name}")
    print(f"{'='*60}")

    image_uri = build_and_push_image(
        args.server_name, args.account_id, args.region
    )

    arn = deploy_to_agentcore(
        server_name=args.server_name,
        image_uri=image_uri,
        region=args.region,
        execution_role_arn=args.execution_role_arn,
        cognito_discovery_url=args.cognito_discovery_url,
        cognito_client_id=args.cognito_client_id,
        header_allowlist=header_allowlist,
    )

    print(f"\n  Deployed ARN: {arn}")

    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"arn={arn}\n")


if __name__ == "__main__":
    main()
