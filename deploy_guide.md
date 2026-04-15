# Deploy MCP servers from a public GitHub repo — step by step

This guide deploys your EFFIS Fire Detection server (and any future MCP servers) to
AWS Bedrock AgentCore via GitHub Actions, with **zero secrets in the repo**.

**Security model:**

| Secret | Where it lives | Who can access it |
|---|---|---|
| AWS credentials | OIDC federation — no keys at all | Only `main` branch workflows |
| AWS Account ID | GitHub Secrets (encrypted) | Repo admins + Actions on `main` |
| Cognito IDs | GitHub Secrets (encrypted) | Same |
| CDSE credentials | GitHub Secrets → injected at build time into S3 deploy zip | Same |

---

## Prerequisites

Run each of these to confirm you have them:

```bash
aws --version          # AWS CLI v2
docker --version       # Docker
python3 --version      # Python 3.10+
gh --version           # GitHub CLI  (brew install gh)
git --version          # Git
```

You also need:
- An AWS account with IAM admin permissions (to create the OIDC trust — one time only)
- A GitHub account
- Your CDSE credentials (from https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings)

---

## Step 1 — Restructure into monorepo layout

You already have `effis/server.py` working. Move it into the `servers/` tree so the
CI/CD pipeline can detect and deploy it.

```bash
cd /Users/antoniolopez/Desktop/pischool/mcp_deployment

# Copy server code into the monorepo structure
cp effis/server.py      servers/effis/server.py
cp effis/requirements.txt servers/effis/requirements.txt

# Copy the shapefile projection file if present
cp -r effis/effis_layer servers/effis/effis_layer 2>/dev/null || true

# Copy tests (not deployed, but useful in repo)
cp effis/test_remote.py servers/effis/test_remote.py
cp effis/test.py        servers/effis/test.py 2>/dev/null || true
```

Verify the layout:

```bash
find servers/ shared/ scripts/ .github/ -type f | sort
```

Expected:

```
.github/workflows/deploy.yml
scripts/deploy_server.py
scripts/detect_changed.py
scripts/setup_oidc.py
servers/effis/.env.template
servers/effis/effis_layer/modis.ba.poly.prj
servers/effis/requirements.txt
servers/effis/server.py
servers/effis/test.py
servers/effis/test_remote.py
shared/Dockerfile
```

---

## Step 2 — Create the GitHub repo (public)

```bash
cd /Users/antoniolopez/Desktop/pischool/mcp_deployment

git init
git add .gitignore shared/ scripts/ servers/ .github/ deploy_guide.md
git commit -m "Initial monorepo structure for MCP servers"
```

Create the public repo on GitHub:

```bash
gh auth login                     # if not already logged in
gh repo create eve-esa/tools --public --source=. --push
```

> The repo `eve-esa/tools` is already created. If starting fresh, replace with your
> actual GitHub org/repo name. This name is used in Step 3 to scope the OIDC trust.

---

## Step 3 — Set up AWS OIDC trust (run once)

This creates the IAM role that GitHub Actions will assume. No long-lived AWS keys
are stored anywhere.

```bash
cd /Users/antoniolopez/Desktop/pischool/mcp_deployment

# Activate your existing venv (has boto3)
source .venv/bin/activate

python scripts/setup_oidc.py \
    --github-repo eve-esa/tools \
    --region eu-west-1
```

The script will print a confirmation with your Account ID and Role ARN. Note them
for the next step.

**What this creates in AWS:**

1. An OIDC identity provider for `token.actions.githubusercontent.com`
2. An IAM role `github-eve-tools-agentcore-deploy` that **only** the `main` branch
   of your repo can assume
3. A least-privilege policy `EveToolsAgentCoreDeployPolicy` allowing ECR push + AgentCore create/update

---

## Step 4 — Add GitHub Secrets

These are encrypted at rest and **never** visible in logs, forks, or PRs.

```bash
# AWS identifiers
gh secret set AWS_ACCOUNT_ID --body "686812424034"
gh secret set AGENTCORE_EXECUTION_ROLE_ARN --body "arn:aws:iam::686812424034:role/AmazonBedrockAgentCoreSDKRuntime-eu-west-1-faf32292cc"

# Cognito (for JWT auth on your MCP servers)
gh secret set COGNITO_DISCOVERY_URL --body "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_PbtACll7l/.well-known/openid-configuration"
gh secret set COGNITO_CLIENT_ID --body "1d33g955n8vglb3g1vfh9s4tau"

# CDSE credentials (for compute_metrics Sentinel Hub access)
gh secret set CDSE_CLIENT_ID --body "YOUR_CDSE_CLIENT_ID"
gh secret set CDSE_CLIENT_SECRET --body "YOUR_CDSE_CLIENT_SECRET"
```

> **Replace** the CDSE values with the real ones from your `.env` file.

Verify they're set:

```bash
gh secret list
```

You should see all 6 secrets listed (values are never shown).

---

## Step 5 — Create a GitHub Environment with protection rules

### What is a GitHub Environment?

A GitHub Environment is a named deployment target (like `production`, `staging`,
`dev`) that you configure on your repo. It acts as a **gate** that a workflow job
must pass through before it can run.

In our workflow (`.github/workflows/deploy.yml`), both the `deploy` and
`smoke-test` jobs declare `environment: production`. This means before those jobs
execute, GitHub checks the rules configured on that environment.

### Why does this matter?

Without an environment, the only protection is the workflow trigger
(`on: push: branches: [main]`). That's usually fine, but an environment adds an
independent second layer:

1. **Branch restriction** — even if someone modifies the workflow trigger on a
   feature branch, the environment blocks non-`main` deployments
2. **Secret scoping** (optional) — you can scope secrets to an environment so
   they're only available to jobs that reference it
3. **Manual approval** (optional, requires GitHub Team/Enterprise for private
   repos) — a human must click "Approve" before each deploy job starts

### Commands

**Command 1 — Create the environment:**

```bash
gh api repos/eve-esa/tools/environments/production \
  --method PUT \
  --input - << 'EOF'
{
  "deployment_branch_policy": {
    "protected_branches": false,
    "custom_branch_policies": true
  }
}
EOF
```

This creates an environment called `production` and sets
`custom_branch_policies: true`, meaning you will manually specify which branches
are allowed to deploy to it.

**Command 2 — Restrict to main:**

```bash
gh api repos/eve-esa/tools/environments/production/deployment-branch-policies \
  --method POST \
  --input - << 'EOF'
{
  "name": "main",
  "type": "branch"
}
EOF
```

This says: only workflows running on the `main` branch can use this environment.
Even if someone modifies the workflow file on a feature branch to say
`environment: production`, GitHub would block it.

### Verify

Go to https://github.com/eve-esa/tools/settings/environments/production and
confirm you see `main` listed under "Deployment branches and tags".

### Optional — require manual approval

Requires GitHub Team or Enterprise plan for private repos. If available:

Go to your repo on GitHub → Settings → Environments → production →
check "Required reviewers" and add yourself. On free plans with private repos
this option is not available — the branch restriction above is still in effect.

---

## Step 6 — Enable branch protection on main

This prevents anyone from pushing directly to `main` or modifying the workflow
without a reviewed PR.

```bash
gh api repos/eve-esa/tools/branches/main/protection \
  --method PUT \
  --input - << 'EOF'
{
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true
  },
  "enforce_admins": true,
  "required_status_checks": null,
  "restrictions": null
}
EOF
```

> **Note:** On free GitHub plans, branch protection for public repos has some
> limitations. The environment protection from Step 5 is the more important gate.

---

## Step 7 — Trigger the first deployment

The workflow triggers on pushes to `main` that modify `servers/**` or `shared/**`.
Since you already pushed in Step 2, and the files are in `servers/`, the workflow
should already be running.

Check it:

```bash
gh run list --limit 3
```

If no run triggered (e.g. the initial push happened before secrets were set), force
a re-deploy by making a small change:

```bash
# Make a trivial edit to trigger the pipeline
echo "" >> servers/effis/requirements.txt
git add servers/effis/requirements.txt
git commit -m "Trigger initial CI/CD deploy"
git push origin main
```

Watch the run:

```bash
gh run watch
```

---

## Step 8 — Verify the deployment

Once the workflow completes, verify your server is running:

```bash
# Check runtime status via AWS CLI
aws bedrock-agentcore get-agent-runtime \
    --agent-runtime-name effis \
    --region eu-west-1 \
    --query 'agentRuntimeStatus' \
    --output text
```

Then run your existing smoke test:

```bash
export BEARER_TOKEN="<your cognito token>"
python servers/effis/test_remote.py
```

---

## How it works (what stays where)

```
┌─────────────────────────────────────────────────────────────────┐
│  PUBLIC GITHUB REPO                                             │
│                                                                 │
│  servers/effis/server.py          ← your MCP server code        │
│  servers/effis/requirements.txt   ← Python dependencies         │
│  servers/effis/.env.template      ← SECRET NAMES only, no vals  │
│  shared/Dockerfile                ← shared build recipe          │
│  scripts/deploy_server.py         ← deploy automation            │
│  .github/workflows/deploy.yml     ← CI/CD pipeline              │
│                                                                 │
│  .gitignore blocks: .env, .venv, .bedrock_agentcore*            │
├─────────────────────────────────────────────────────────────────┤
│  GITHUB SECRETS (encrypted, never in code or logs)              │
│                                                                 │
│  AWS_ACCOUNT_ID, AGENTCORE_EXECUTION_ROLE_ARN                   │
│  COGNITO_DISCOVERY_URL, COGNITO_CLIENT_ID                       │
│  CDSE_CLIENT_ID, CDSE_CLIENT_SECRET                             │
├─────────────────────────────────────────────────────────────────┤
│  AWS (accessed via OIDC, no stored keys)                        │
│                                                                 │
│  IAM Role: github-eve-tools-agentcore-deploy                    │
│    └─ Trust: only main branch of eve-esa/tools                  │
│    └─ Policy: EveToolsAgentCoreDeployPolicy (scoped)            │
│  ECR: mcp-servers/effis (private image registry)                │
│  AgentCore: runtime "effis" with Cognito JWT auth               │
└─────────────────────────────────────────────────────────────────┘
```

**Secret injection flow during CI:**

```
GitHub Secret CDSE_CLIENT_ID
       │
       ▼
  envsubst fills .env.template → .env   (ephemeral CI runner only)
       │
       ▼
  COPY . .  in Dockerfile → .env is in the image
       │
       ▼
  docker push → private ECR (encrypted at rest)
       │
       ▼
  AgentCore runs container → load_dotenv() finds .env → os.getenv() works
```

---

## Adding a new MCP server

1. Create the server folder:

```bash
mkdir servers/my-new-tool
```

2. Add `server.py` and `requirements.txt`:

```bash
cat > servers/my-new-tool/server.py << 'PYEOF'
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("My New Tool", host="0.0.0.0", port=8000, stateless_http=True)

@mcp.tool()
def hello(name: str) -> str:
    """Say hello."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
PYEOF

echo "mcp[cli]>=1.2.0" > servers/my-new-tool/requirements.txt
```

3. (Optional) If the server needs secrets, create `.env.template`:

```bash
echo 'MY_API_KEY=${MY_API_KEY}' > servers/my-new-tool/.env.template
gh secret set MY_API_KEY --body "actual-key-value"
```

Then add the variable to the "Inject runtime secrets" step in `.github/workflows/deploy.yml`:

```yaml
env:
  CDSE_CLIENT_ID: ${{ secrets.CDSE_CLIENT_ID }}
  CDSE_CLIENT_SECRET: ${{ secrets.CDSE_CLIENT_SECRET }}
  MY_API_KEY: ${{ secrets.MY_API_KEY }}           # ← add this
```

4. Push:

```bash
git add servers/my-new-tool
git commit -m "Add my-new-tool MCP server"
git push origin main
```

The pipeline detects only the changed folder, builds it, and deploys to AgentCore.

---

## Teardown / cleanup

If you need to remove a server:

```bash
# Delete the AgentCore runtime
aws bedrock-agentcore delete-agent-runtime \
    --agent-runtime-name my-new-tool \
    --region eu-west-1

# Delete the ECR repo
aws ecr delete-repository \
    --repository-name mcp-servers/my-new-tool \
    --region eu-west-1 \
    --force

# Remove from repo
rm -rf servers/my-new-tool
git add -A && git commit -m "Remove my-new-tool" && git push
```
