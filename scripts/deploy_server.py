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
        [--cognito-client-id abc123]
"""

import argparse
import json
import os
import subprocess
import sys

import boto3


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
        check=True,
    )

    print(f"  Building image for {server_name}...")
    subprocess.run(
        [
            "docker", "build",
            "-f", "shared/Dockerfile",
            "-t", image_uri,
            f"servers/{server_name}",
        ],
        check=True,
    )

    print(f"  Pushing to ECR...")
    subprocess.run(["docker", "push", image_uri], check=True)

    return image_uri


def deploy_to_agentcore(
    server_name: str,
    image_uri: str,
    region: str,
    execution_role_arn: str,
    cognito_discovery_url: str | None = None,
    cognito_client_id: str | None = None,
) -> str:
    client = boto3.client("bedrock-agentcore", region_name=region)

    try:
        existing = client.get_agent_runtime(agentRuntimeName=server_name)
        runtime_arn = existing["agentRuntimeArn"]
        print(f"  Updating existing runtime: {runtime_arn}")

        client.update_agent_runtime(
            agentRuntimeName=server_name,
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": image_uri}
            },
        )
        print(f"  Updated:  {runtime_arn}")
        return runtime_arn

    except client.exceptions.ResourceNotFoundException:
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

        if cognito_discovery_url and cognito_client_id:
            create_kwargs["authorizerConfiguration"] = {
                "customJWTAuthorizer": {
                    "discoveryUrl": cognito_discovery_url,
                    "allowedClients": [cognito_client_id],
                }
            }

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
    args = parser.parse_args()

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
    )

    print(f"\n  Deployed ARN: {arn}")

    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"arn={arn}\n")


if __name__ == "__main__":
    main()
