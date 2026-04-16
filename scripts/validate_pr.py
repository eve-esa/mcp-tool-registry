"""
Validate a pull request against the MCP server monorepo conventions.

Checks performed (exit code 1 on any failure):
    1. Single-server scope — PR touches at most one server directory
    2. Required files      — server.py and requirements.txt exist
    3. No secrets          — no .env, .pem, .key, credentials committed
    4. Entrypoint schema   — server.py follows the AgentCore contract
    5. requirements.txt    — non-empty, all deps have version constraints
    6. .env.template       — only ${PLACEHOLDER} patterns, no leaked secrets
    7. File size guard     — no file exceeds MAX_FILE_SIZE_MB
    8. Hardcoded secrets   — scan source files for inline credentials

Usage:
    python scripts/validate_pr.py                     # auto-detect via git diff
    python scripts/validate_pr.py --base-ref origin/main
    python scripts/validate_pr.py --files f1 f2 ...   # explicit file list
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

MAX_FILE_SIZE_MB = 5
REQUIRED_SERVER_FILES = ["server.py", "requirements.txt"]
FORBIDDEN_FILE_PATTERNS = [
    "*.pem",
    "*.key",
    "credentials.json",
    "token.json",
    "*.p12",
    "*.pfx",
]
INFRA_PATHS = {"shared", "scripts", ".github"}

# Patterns that match hardcoded secrets in source code.
# Each tuple: (compiled regex, human-readable description).
# The regexes run per-line on .py files in the changed server directory.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"""(?:password|passwd|pwd)\s*=\s*["'][^"']{8,}["']""", re.IGNORECASE),
        "hardcoded password assignment",
    ),
    (
        re.compile(r"""(?:api_key|apikey|api_secret)\s*=\s*["'][^"']{8,}["']""", re.IGNORECASE),
        "hardcoded API key",
    ),
    (
        re.compile(r"""(?:secret_key|secret)\s*=\s*["'][^"']{16,}["']""", re.IGNORECASE),
        "hardcoded secret key",
    ),
    (
        re.compile(r"""(?:access_token|auth_token|bearer)\s*=\s*["'][^"']{16,}["']""", re.IGNORECASE),
        "hardcoded access/auth token",
    ),
    (
        re.compile(r"""["']AKIA[0-9A-Z]{16}["']"""),
        "AWS access key ID (AKIA...)",
    ),
    (
        re.compile(r"""["'][0-9a-zA-Z/+]{40}["']"""),
        "possible AWS secret access key (40-char base64)",
    ),
    (
        re.compile(r"""(?:Authorization|Bearer)\s*["']Bearer\s+[A-Za-z0-9\-._~+/]+=*["']""", re.IGNORECASE),
        "hardcoded Bearer token",
    ),
    (
        re.compile(r"""["']ghp_[0-9a-zA-Z]{36}["']"""),
        "GitHub personal access token",
    ),
    (
        re.compile(r"""["']sk-[0-9a-zA-Z\-_]{20,}["']"""),
        "OpenAI / Stripe secret key",
    ),
]
# Lines matching these patterns are false-positive suppressions:
# doc strings, comments, example/placeholder values, os.environ reads.
_SECRET_FALSE_POSITIVE = re.compile(
    r"""(^\s*#|^\s*["']{3}|os\.environ|os\.getenv|"""
    r"""getenv\(|\.get\(|"""
    r"""\$\{|<[A-Z_]+>|"""
    r"""example|placeholder|your[_-]|changeme|xxx|dummy|"""
    r"""REPLACE_ME)""",
    re.IGNORECASE,
)


class ValidationError:
    def __init__(self, check: str, message: str) -> None:
        self.check = check
        self.message = message

    def __str__(self) -> str:
        return f"[{self.check}] {self.message}"


def get_changed_files(base_ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base_ref, "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [f for f in result.stdout.strip().split("\n") if f]


def extract_server_names(changed_files: list[str]) -> set[str]:
    servers: set[str] = set()
    for filepath in changed_files:
        parts = Path(filepath).parts
        if len(parts) >= 2 and parts[0] == "servers":
            servers.add(parts[1])
    return servers


def has_infra_changes(changed_files: list[str]) -> list[str]:
    hits: list[str] = []
    for filepath in changed_files:
        top_dir = Path(filepath).parts[0] if Path(filepath).parts else ""
        if top_dir in INFRA_PATHS:
            hits.append(filepath)
    return hits


def check_single_server(changed_files: list[str]) -> list[ValidationError]:
    servers = extract_server_names(changed_files)
    if len(servers) > 1:
        names = ", ".join(sorted(servers))
        return [
            ValidationError(
                "single-server",
                f"PR modifies {len(servers)} servers ({names}). "
                f"Each PR must touch at most one server directory.",
            )
        ]
    return []


def check_required_files(server_name: str) -> list[ValidationError]:
    errors: list[ValidationError] = []
    server_dir = Path("servers") / server_name
    for filename in REQUIRED_SERVER_FILES:
        if not (server_dir / filename).exists():
            errors.append(
                ValidationError(
                    "required-files",
                    f"servers/{server_name}/{filename} is missing. "
                    f"Every server must include {', '.join(REQUIRED_SERVER_FILES)}.",
                )
            )
    return errors


def check_no_secrets(changed_files: list[str]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for filepath in changed_files:
        p = Path(filepath)

        if p.name == ".env" and p.parts[0] == "servers":
            errors.append(
                ValidationError(
                    "no-secrets",
                    f"{filepath}: .env files must not be committed. "
                    f"Use .env.template with ${{PLACEHOLDER}} syntax instead.",
                )
            )

        for pattern in FORBIDDEN_FILE_PATTERNS:
            if p.match(pattern):
                errors.append(
                    ValidationError(
                        "no-secrets",
                        f"{filepath}: files matching '{pattern}' must not be committed.",
                    )
                )
                break
    return errors


def check_entrypoint_schema(server_name: str) -> list[ValidationError]:
    errors: list[ValidationError] = []
    server_py = Path("servers") / server_name / "server.py"
    if not server_py.exists():
        return errors  # already caught by required-files check

    content = server_py.read_text(encoding="utf-8")

    if not re.search(r"FastMCP\s*\(", content):
        errors.append(
            ValidationError(
                "entrypoint-schema",
                f"servers/{server_name}/server.py: missing FastMCP(...) instantiation.",
            )
        )

    if 'host="0.0.0.0"' not in content and "host='0.0.0.0'" not in content:
        errors.append(
            ValidationError(
                "entrypoint-schema",
                f'servers/{server_name}/server.py: must bind to host="0.0.0.0" '
                f"for container networking.",
            )
        )

    if "port=8000" not in content:
        errors.append(
            ValidationError(
                "entrypoint-schema",
                f"servers/{server_name}/server.py: must use port=8000 "
                f"(matches shared/Dockerfile EXPOSE 8000).",
            )
        )

    if "stateless_http=True" not in content:
        errors.append(
            ValidationError(
                "entrypoint-schema",
                f"servers/{server_name}/server.py: must set stateless_http=True "
                f"(required for AgentCore).",
            )
        )

    if 'transport="streamable-http"' not in content and "transport='streamable-http'" not in content:
        errors.append(
            ValidationError(
                "entrypoint-schema",
                f"servers/{server_name}/server.py: must call "
                f'mcp.run(transport="streamable-http") in the __main__ block.',
            )
        )

    return errors


def check_requirements_txt(server_name: str) -> list[ValidationError]:
    errors: list[ValidationError] = []
    req_path = Path("servers") / server_name / "requirements.txt"
    if not req_path.exists():
        return errors

    lines = req_path.read_text(encoding="utf-8").strip().splitlines()
    non_empty = [
        ln.strip()
        for ln in lines
        if ln.strip() and not ln.strip().startswith("#")
    ]

    if not non_empty:
        errors.append(
            ValidationError(
                "requirements-txt",
                f"servers/{server_name}/requirements.txt is empty.",
            )
        )
        return errors

    version_re = re.compile(r"[><=!~]")
    for line in non_empty:
        if line.startswith("-e ") or line.startswith("file:"):
            errors.append(
                ValidationError(
                    "requirements-txt",
                    f"servers/{server_name}/requirements.txt: "
                    f"local/editable installs are not allowed: '{line}'",
                )
            )
            continue

        pkg_part = line.split(";")[0].strip()
        if not version_re.search(pkg_part):
            errors.append(
                ValidationError(
                    "requirements-txt",
                    f"servers/{server_name}/requirements.txt: "
                    f"'{line}' has no version constraint. "
                    f"Pin at least a minimum version (e.g. httpx>=0.27.0).",
                )
            )

    return errors


def check_env_template(server_name: str) -> list[ValidationError]:
    errors: list[ValidationError] = []
    tmpl_path = Path("servers") / server_name / ".env.template"
    if not tmpl_path.exists():
        return errors

    placeholder_re = re.compile(r"\$\{[A-Z_][A-Z0-9_]*\}")
    suspicious_value_re = re.compile(r"^[A-Za-z0-9+/=]{20,}$")

    for i, raw_line in enumerate(tmpl_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            errors.append(
                ValidationError(
                    "env-template",
                    f"servers/{server_name}/.env.template line {i}: "
                    f"expected KEY=VALUE format, got: '{line}'",
                )
            )
            continue

        _, value = line.split("=", 1)
        value = value.strip()

        if placeholder_re.fullmatch(value):
            continue

        if suspicious_value_re.match(value):
            errors.append(
                ValidationError(
                    "env-template",
                    f"servers/{server_name}/.env.template line {i}: "
                    f"value looks like a hardcoded secret. "
                    f"Use ${{SECRET_NAME}} placeholders instead.",
                )
            )

    return errors


def check_file_sizes(changed_files: list[str]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    for filepath in changed_files:
        p = Path(filepath)
        if p.exists() and p.stat().st_size > max_bytes:
            size_mb = p.stat().st_size / (1024 * 1024)
            errors.append(
                ValidationError(
                    "file-size",
                    f"{filepath}: {size_mb:.1f} MB exceeds the "
                    f"{MAX_FILE_SIZE_MB} MB limit.",
                )
            )
    return errors


def check_hardcoded_secrets(changed_files: list[str]) -> list[ValidationError]:
    """Scan changed .py files for patterns that look like inline credentials."""
    errors: list[ValidationError] = []
    for filepath in changed_files:
        p = Path(filepath)
        if p.suffix != ".py" or not p.exists():
            continue

        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue

        for lineno, line in enumerate(lines, 1):
            if _SECRET_FALSE_POSITIVE.search(line):
                continue
            for pattern, description in _SECRET_PATTERNS:
                if pattern.search(line):
                    snippet = line.strip()
                    if len(snippet) > 120:
                        snippet = snippet[:117] + "..."
                    errors.append(
                        ValidationError(
                            "hardcoded-secret",
                            f"{filepath}:{lineno}: {description}  →  {snippet}",
                        )
                    )
                    break  # one hit per line is enough

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate PR against monorepo conventions")
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref to diff against (default: origin/main)",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        help="Explicit list of changed files (skips git diff)",
    )
    args = parser.parse_args()

    if args.files is not None:
        changed_files = args.files
    else:
        changed_files = get_changed_files(args.base_ref)

    if not changed_files:
        print("No changed files detected — nothing to validate.")
        sys.exit(0)

    all_errors: list[ValidationError] = []

    all_errors.extend(check_single_server(changed_files))
    all_errors.extend(check_no_secrets(changed_files))
    all_errors.extend(check_file_sizes(changed_files))
    all_errors.extend(check_hardcoded_secrets(changed_files))

    servers = extract_server_names(changed_files)
    for server_name in servers:
        all_errors.extend(check_required_files(server_name))
        all_errors.extend(check_entrypoint_schema(server_name))
        all_errors.extend(check_requirements_txt(server_name))
        all_errors.extend(check_env_template(server_name))

    infra_files = has_infra_changes(changed_files)
    if infra_files:
        print(f"::warning::PR modifies shared infrastructure: {', '.join(infra_files)}")

    if all_errors:
        print(f"\n{'='*60}")
        print(f"  PR VALIDATION FAILED — {len(all_errors)} error(s)")
        print(f"{'='*60}\n")
        for err in all_errors:
            print(f"  ✗ {err}")
        print()
        sys.exit(1)
    else:
        print(f"\n{'='*60}")
        print("  PR VALIDATION PASSED")
        print(f"{'='*60}")
        print(f"  Changed files: {len(changed_files)}")
        print(f"  Servers touched: {', '.join(sorted(servers)) or 'none'}")
        if infra_files:
            print("  ⚠ Infrastructure files changed (requires maintainer review)")
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
