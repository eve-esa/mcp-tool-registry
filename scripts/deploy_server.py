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
        [--runtime-name my_server]
        [--image-uri 123.dkr.ecr.eu-west-1.amazonaws.com/mcp-servers/my_server:abc]
        [--skip-deploy]
        [--environment-variables '{"EXAMPLE_VAR":"value"}']
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

import boto3

DEFAULT_HEADER_ALLOWLIST: dict[str, list[str]] = {
    "effis": ["X-CDSE-Client-Id", "X-CDSE-Client-Secret"],
    "serpapi": ["X-API-Key"],
    "eve_retrieval": ["X-EVE-Token"],
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


def _write_github_output(**kwargs: str) -> None:
    github_output = os.getenv("GITHUB_OUTPUT")
    if not github_output:
        return
    with open(github_output, "a") as f:
        for key, value in kwargs.items():
            f.write(f"{key}={value}\n")


def _parse_environment_variables(raw: str | None) -> dict[str, str] | None:
    """Parse ``--environment-variables`` as a JSON object or ``@filepath``."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("@"):
        text = Path(text[1:]).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--environment-variables must be valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("--environment-variables must be a JSON object")
    parsed = {str(k): str(v) for k, v in data.items()}
    return parsed or None


def build_and_push_image(
    server_name: str,
    account_id: str,
    region: str,
    image_tag: str = "latest",
) -> str:
    ecr_repo = f"mcp-servers/{server_name}"
    registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    image_uri = f"{registry}/{ecr_repo}:{image_tag}"
    latest_uri = f"{registry}/{ecr_repo}:latest"

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
            registry,
        ],
        input=login_pw.stdout,
        text=True,
        check=True,
    )

    dockerfile, build_context = _resolve_dockerfile(server_name)
    print(f"  Building image for {server_name} (linux/arm64)...")
    build_cmd = [
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/arm64",
        "-f",
        dockerfile,
        "-t",
        image_uri,
    ]
    if image_tag != "latest":
        build_cmd.extend(["-t", latest_uri])
    build_cmd.extend(["--load", build_context])
    subprocess.run(build_cmd, check=True)

    print("  Pushing to ECR...")
    subprocess.run(["docker", "push", image_uri], check=True)
    if image_tag != "latest":
        subprocess.run(["docker", "push", latest_uri], check=True)

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
    """Return default header allowlist for known servers (folder name, not runtime name)."""
    return DEFAULT_HEADER_ALLOWLIST.get(server_name)


def deploy_to_agentcore(
    runtime_name: str,
    image_uri: str,
    region: str,
    execution_role_arn: str,
    cognito_discovery_url: str | None = None,
    cognito_client_id: str | None = None,
    header_allowlist: list[str] | None = None,
    environment_variables: dict[str, str] | None = None,
) -> str:
    client = boto3.client("bedrock-agentcore-control", region_name=region)

    existing = find_runtime_by_name(client, runtime_name)
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

        if environment_variables:
            update_kwargs["environmentVariables"] = environment_variables

        client.update_agent_runtime(**update_kwargs)
        print(f"  Updated:  {runtime_arn}")
        return runtime_arn

    print(f"  Creating new runtime: {runtime_name}")

    create_kwargs = {
        "agentRuntimeName": runtime_name,
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

    if environment_variables:
        create_kwargs["environmentVariables"] = environment_variables

    response = client.create_agent_runtime(**create_kwargs)
    runtime_arn = response["agentRuntimeArn"]
    print(f"  Created:  {runtime_arn}")
    return runtime_arn


def main():
    parser = argparse.ArgumentParser(description="Deploy an MCP server to AgentCore")
    parser.add_argument("--server-name", required=True)
    parser.add_argument(
        "--runtime-name",
        default=None,
        help="AgentCore runtime name (default: --server-name)",
    )
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--account-id", required=True)
    parser.add_argument(
        "--execution-role-arn",
        default=None,
        help="Required unless --skip-deploy is set",
    )
    parser.add_argument("--cognito-discovery-url", default=None)
    parser.add_argument("--cognito-client-id", default=None)
    parser.add_argument(
        "--header-allowlist",
        default=None,
        help="Comma-separated list of headers to allow (e.g. 'X-API-Key,X-Custom-Header')",
    )
    parser.add_argument(
        "--image-uri",
        default=None,
        help="Skip Docker/ECR and deploy this image URI",
    )
    parser.add_argument(
        "--image-tag",
        default="latest",
        help="ECR image tag when building (default: latest). Also pushes :latest.",
    )
    parser.add_argument(
        "--skip-deploy",
        action="store_true",
        help="Build and push the image only; do not create/update AgentCore",
    )
    parser.add_argument(
        "--environment-variables",
        default=None,
        help='JSON object of AgentCore env vars, or @path to a JSON file',
    )
    args = parser.parse_args()

    if args.image_uri is not None and not args.image_uri.strip():
        parser.error("--image-uri must not be empty")
    if args.skip_deploy and args.image_uri:
        parser.error("--skip-deploy and --image-uri cannot be used together")
    if not args.skip_deploy and not args.execution_role_arn:
        parser.error("--execution-role-arn is required unless --skip-deploy is set")

    runtime_name = args.runtime_name or args.server_name
    environment_variables = _parse_environment_variables(args.environment_variables)

    header_allowlist = None
    if args.header_allowlist:
        header_allowlist = [h.strip() for h in args.header_allowlist.split(",") if h.strip()]
    else:
        header_allowlist = _get_default_headers_for_server(args.server_name)
        if header_allowlist:
            print(f"  Using default headers for {args.server_name}: {header_allowlist}")

    print(f"\n{'='*60}")
    print(f"  MCP server: {args.server_name}")
    if runtime_name != args.server_name:
        print(f"  Runtime:    {runtime_name}")
    print(f"{'='*60}")

    if args.image_uri:
        image_uri = args.image_uri
        print(f"  Using existing image: {image_uri}")
    else:
        image_uri = build_and_push_image(
            args.server_name, args.account_id, args.region, args.image_tag
        )
        _write_github_output(image_tag=args.image_tag)

    if args.skip_deploy:
        print(f"\n  Built image: {image_uri}")
        print("  Skipping AgentCore deploy (--skip-deploy)")
        return

    if environment_variables:
        print(f"  Environment variables: {sorted(environment_variables)}")

    arn = deploy_to_agentcore(
        runtime_name=runtime_name,
        image_uri=image_uri,
        region=args.region,
        execution_role_arn=args.execution_role_arn,
        cognito_discovery_url=args.cognito_discovery_url,
        cognito_client_id=args.cognito_client_id,
        header_allowlist=header_allowlist,
        environment_variables=environment_variables,
    )

    print(f"\n  Deployed ARN: {arn}")
    _write_github_output(arn=arn)


if __name__ == "__main__":
    main()
