"""
Test suite for the PR validation checks (scripts/validate_pr.py).

Creates temporary broken server fixtures, runs the validator against them,
and asserts the correct checks fire.  Every test is self-contained — no
external setup or running CI is required.

Usage:
    python -m pytest tests/test_pr_checks.py -v        # from tools/
    python tests/test_pr_checks.py                      # standalone
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from validate_pr import (  # noqa: E402, I001
    check_entrypoint_schema,
    check_env_template,
    check_file_sizes,
    check_hardcoded_secrets,
    check_no_secrets,
    check_requirements_txt,
    check_required_files,
    check_single_server,
    extract_server_names,
    has_infra_changes,
)

VALID_SERVER_PY = textwrap.dedent("""\
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("Test", host="0.0.0.0", port=8000, stateless_http=True)

    @mcp.tool()
    async def ping() -> str:
        return "pong"

    if __name__ == "__main__":
        mcp.run(transport="streamable-http")
""")

VALID_REQUIREMENTS = "mcp[cli]>=1.2.0\nhttpx>=0.27.0\n"


@pytest.fixture()
def server_dir(tmp_path: Path):
    """Create a minimal valid server inside a temporary servers/ tree.

    Returns a helper object with the paths and a method to run checks
    from the perspective of the temporary repo root.
    """
    repo = tmp_path / "repo"
    sdir = repo / "servers" / "test-server"
    sdir.mkdir(parents=True)
    (sdir / "server.py").write_text(VALID_SERVER_PY)
    (sdir / "requirements.txt").write_text(VALID_REQUIREMENTS)

    import os
    orig = os.getcwd()
    os.chdir(repo)
    yield sdir
    os.chdir(orig)


# ─── 1. Single-server scope ────────────────────────────────────────────


class TestSingleServer:
    def test_one_server_passes(self):
        files = ["servers/alpha/server.py", "servers/alpha/requirements.txt"]
        assert check_single_server(files) == []

    def test_two_servers_fails(self):
        files = ["servers/alpha/server.py", "servers/beta/server.py"]
        errors = check_single_server(files)
        assert len(errors) == 1
        assert "single-server" in errors[0].check
        assert "alpha" in errors[0].message
        assert "beta" in errors[0].message

    def test_three_servers_fails(self):
        files = [
            "servers/a/server.py",
            "servers/b/server.py",
            "servers/c/server.py",
        ]
        errors = check_single_server(files)
        assert len(errors) == 1
        assert "3 servers" in errors[0].message

    def test_non_server_files_pass(self):
        files = ["README.md", "shared/Dockerfile"]
        assert check_single_server(files) == []

    def test_one_server_plus_infra_passes(self):
        files = ["servers/alpha/server.py", "shared/Dockerfile"]
        assert check_single_server(files) == []


# ─── 2. Required files ─────────────────────────────────────────────────


class TestRequiredFiles:
    def test_both_present_passes(self, server_dir: Path):
        assert check_required_files("test-server") == []

    def test_missing_server_py(self, server_dir: Path):
        (server_dir / "server.py").unlink()
        errors = check_required_files("test-server")
        assert any("server.py" in e.message for e in errors)

    def test_missing_requirements(self, server_dir: Path):
        (server_dir / "requirements.txt").unlink()
        errors = check_required_files("test-server")
        assert any("requirements.txt" in e.message for e in errors)

    def test_both_missing(self, server_dir: Path):
        (server_dir / "server.py").unlink()
        (server_dir / "requirements.txt").unlink()
        errors = check_required_files("test-server")
        assert len(errors) == 2


# ─── 3. No secrets (file-level) ────────────────────────────────────────


class TestNoSecrets:
    def test_env_file_rejected(self):
        errors = check_no_secrets(["servers/alpha/.env"])
        assert len(errors) == 1
        assert "no-secrets" in errors[0].check

    def test_env_template_allowed(self):
        assert check_no_secrets(["servers/alpha/.env.template"]) == []

    def test_pem_rejected(self):
        errors = check_no_secrets(["servers/alpha/cert.pem"])
        assert any("*.pem" in e.message for e in errors)

    def test_key_rejected(self):
        errors = check_no_secrets(["servers/alpha/private.key"])
        assert any("*.key" in e.message for e in errors)

    def test_credentials_json_rejected(self):
        errors = check_no_secrets(["servers/alpha/credentials.json"])
        assert any("credentials.json" in e.message for e in errors)

    def test_p12_rejected(self):
        errors = check_no_secrets(["servers/alpha/cert.p12"])
        assert any("*.p12" in e.message for e in errors)

    def test_normal_files_pass(self):
        files = ["servers/alpha/server.py", "servers/alpha/utils.py"]
        assert check_no_secrets(files) == []


# ─── 4. Entrypoint schema ──────────────────────────────────────────────


class TestEntrypointSchema:
    def test_valid_server_passes(self, server_dir: Path):
        assert check_entrypoint_schema("test-server") == []

    def test_missing_fastmcp(self, server_dir: Path):
        (server_dir / "server.py").write_text("print('hello')\n")
        errors = check_entrypoint_schema("test-server")
        assert any("FastMCP" in e.message for e in errors)

    def test_wrong_host(self, server_dir: Path):
        bad = VALID_SERVER_PY.replace('host="0.0.0.0"', 'host="127.0.0.1"')
        (server_dir / "server.py").write_text(bad)
        errors = check_entrypoint_schema("test-server")
        assert any("0.0.0.0" in e.message for e in errors)

    def test_wrong_port(self, server_dir: Path):
        bad = VALID_SERVER_PY.replace("port=8000", "port=3000")
        (server_dir / "server.py").write_text(bad)
        errors = check_entrypoint_schema("test-server")
        assert any("port=8000" in e.message for e in errors)

    def test_missing_stateless_http(self, server_dir: Path):
        bad = VALID_SERVER_PY.replace("stateless_http=True", "")
        (server_dir / "server.py").write_text(bad)
        errors = check_entrypoint_schema("test-server")
        assert any("stateless_http" in e.message for e in errors)

    def test_missing_streamable_http(self, server_dir: Path):
        bad = VALID_SERVER_PY.replace(
            'transport="streamable-http"', 'transport="sse"'
        )
        (server_dir / "server.py").write_text(bad)
        errors = check_entrypoint_schema("test-server")
        assert any("streamable-http" in e.message for e in errors)

    def test_all_violations_at_once(self, server_dir: Path):
        (server_dir / "server.py").write_text("x = 1\n")
        errors = check_entrypoint_schema("test-server")
        assert len(errors) == 5  # FastMCP, host, port, stateless, transport


# ─── 5. requirements.txt ───────────────────────────────────────────────


class TestRequirementsTxt:
    def test_valid_passes(self, server_dir: Path):
        assert check_requirements_txt("test-server") == []

    def test_empty_file(self, server_dir: Path):
        (server_dir / "requirements.txt").write_text("# only comments\n")
        errors = check_requirements_txt("test-server")
        assert any("empty" in e.message for e in errors)

    def test_bare_package_name(self, server_dir: Path):
        (server_dir / "requirements.txt").write_text("httpx\nrequests\n")
        errors = check_requirements_txt("test-server")
        assert len(errors) == 2
        assert all("no version constraint" in e.message for e in errors)

    def test_mixed_good_and_bad(self, server_dir: Path):
        content = "mcp[cli]>=1.2.0\nhttpx\npython-dotenv>=1.0.0\n"
        (server_dir / "requirements.txt").write_text(content)
        errors = check_requirements_txt("test-server")
        assert len(errors) == 1
        assert "httpx" in errors[0].message

    def test_editable_install_rejected(self, server_dir: Path):
        (server_dir / "requirements.txt").write_text("-e .\n")
        errors = check_requirements_txt("test-server")
        assert any("editable" in e.message for e in errors)

    def test_file_uri_rejected(self, server_dir: Path):
        (server_dir / "requirements.txt").write_text(
            "file:///home/user/my_pkg\n"
        )
        errors = check_requirements_txt("test-server")
        assert any("local" in e.message for e in errors)

    def test_pinned_exact_passes(self, server_dir: Path):
        (server_dir / "requirements.txt").write_text("httpx==0.27.0\n")
        assert check_requirements_txt("test-server") == []

    def test_environment_marker_passes(self, server_dir: Path):
        content = 'uvloop>=0.19.0; sys_platform != "win32"\n'
        (server_dir / "requirements.txt").write_text(content)
        assert check_requirements_txt("test-server") == []


# ─── 6. .env.template ──────────────────────────────────────────────────


class TestEnvTemplate:
    def test_valid_template_passes(self, server_dir: Path):
        content = "MY_KEY=${MY_KEY}\nAPI_URL=https://api.example.com\n"
        (server_dir / ".env.template").write_text(content)
        assert check_env_template("test-server") == []

    def test_no_template_passes(self, server_dir: Path):
        assert check_env_template("test-server") == []

    def test_hardcoded_secret_detected(self, server_dir: Path):
        content = "MY_KEY=aB3dEfGhIjKlMnOpQrStUvWx\n"
        (server_dir / ".env.template").write_text(content)
        errors = check_env_template("test-server")
        assert any("hardcoded secret" in e.message for e in errors)

    def test_missing_equals_sign(self, server_dir: Path):
        (server_dir / ".env.template").write_text("BROKEN_LINE\n")
        errors = check_env_template("test-server")
        assert any("KEY=VALUE" in e.message for e in errors)

    def test_comments_and_blanks_ignored(self, server_dir: Path):
        content = "# comment\n\nMY_KEY=${MY_KEY}\n"
        (server_dir / ".env.template").write_text(content)
        assert check_env_template("test-server") == []


# ─── 7. File size guard ─────────────────────────────────────────────────


class TestFileSize:
    def test_small_file_passes(self, tmp_path: Path):
        f = tmp_path / "small.py"
        f.write_text("x = 1\n")
        assert check_file_sizes([str(f)]) == []

    def test_large_file_fails(self, tmp_path: Path):
        f = tmp_path / "big.bin"
        f.write_bytes(b"\0" * (6 * 1024 * 1024))  # 6 MB
        errors = check_file_sizes([str(f)])
        assert len(errors) == 1
        assert "file-size" in errors[0].check

    def test_nonexistent_file_passes(self):
        assert check_file_sizes(["does/not/exist.py"]) == []


# ─── 8. Hardcoded secrets in source code ────────────────────────────────


class TestHardcodedSecrets:
    def _write_and_check(self, tmp_path: Path, code: str) -> list:
        f = tmp_path / "server.py"
        f.write_text(textwrap.dedent(code))
        return check_hardcoded_secrets([str(f)])

    def test_hardcoded_password(self, tmp_path: Path):
        errors = self._write_and_check(tmp_path, '''\
            password = "SuperSecretPass123"
        ''')
        assert len(errors) == 1
        assert "password" in errors[0].message

    def test_hardcoded_api_key(self, tmp_path: Path):
        errors = self._write_and_check(tmp_path, '''\
            api_key = "abcdef1234567890"
        ''')
        assert len(errors) == 1
        assert "API key" in errors[0].message

    def test_openai_key(self, tmp_path: Path):
        errors = self._write_and_check(tmp_path, '''\
            key = "sk-proj-abc123def456ghi789jklmnop"
        ''')
        assert len(errors) == 1
        assert "OpenAI" in errors[0].message

    def test_github_pat(self, tmp_path: Path):
        errors = self._write_and_check(tmp_path, '''\
            token = "ghp_ABCDEFghijklmnopqrstuvwxyz012345ABCD"
        ''')
        assert len(errors) == 1
        assert "GitHub" in errors[0].message

    def test_aws_access_key(self, tmp_path: Path):
        errors = self._write_and_check(tmp_path, '''\
            key = "AKIAI44QH8DHBFNRA4QZ"
        ''')
        assert len(errors) == 1
        assert "AWS" in errors[0].message

    def test_os_environ_is_safe(self, tmp_path: Path):
        errors = self._write_and_check(tmp_path, '''\
            password = os.environ["DB_PASSWORD"]
            api_key = os.getenv("API_KEY", "")
        ''')
        assert errors == []

    def test_comment_is_safe(self, tmp_path: Path):
        errors = self._write_and_check(tmp_path, '''\
            # password = "SuperSecretPass123"
        ''')
        assert errors == []

    def test_placeholder_is_safe(self, tmp_path: Path):
        errors = self._write_and_check(tmp_path, '''\
            password = "your-password-here"
            api_key = "changeme12345678"
        ''')
        assert errors == []

    def test_clean_code_passes(self, tmp_path: Path):
        errors = self._write_and_check(tmp_path, '''\
            import os
            from mcp.server.fastmcp import FastMCP
            mcp = FastMCP("Test", host="0.0.0.0", port=8000, stateless_http=True)

            @mcp.tool()
            async def ping() -> str:
                return "pong"
        ''')
        assert errors == []

    def test_non_py_files_skipped(self, tmp_path: Path):
        f = tmp_path / "config.yaml"
        f.write_text('password: "SuperSecretPass123"\n')
        assert check_hardcoded_secrets([str(f)]) == []


# ─── 9. Infra change detection ─────────────────────────────────────────


class TestInfraChanges:
    def test_shared_detected(self):
        assert has_infra_changes(["shared/Dockerfile"]) == ["shared/Dockerfile"]

    def test_scripts_detected(self):
        hits = has_infra_changes(["scripts/deploy_server.py"])
        assert "scripts/deploy_server.py" in hits

    def test_github_detected(self):
        hits = has_infra_changes([".github/workflows/deploy.yml"])
        assert ".github/workflows/deploy.yml" in hits

    def test_server_files_not_flagged(self):
        assert has_infra_changes(["servers/alpha/server.py"]) == []

    def test_mixed(self):
        files = [
            "servers/alpha/server.py",
            "shared/Dockerfile",
            "README.md",
        ]
        hits = has_infra_changes(files)
        assert hits == ["shared/Dockerfile"]


# ─── 10. extract_server_names ───────────────────────────────────────────


class TestExtractServerNames:
    def test_basic(self):
        files = ["servers/foo/server.py", "servers/foo/test.py"]
        assert extract_server_names(files) == {"foo"}

    def test_multiple(self):
        files = ["servers/a/x.py", "servers/b/y.py"]
        assert extract_server_names(files) == {"a", "b"}

    def test_non_server_paths(self):
        assert extract_server_names(["README.md", "shared/Dockerfile"]) == set()

    def test_deeply_nested(self):
        files = ["servers/deep/sub/module/file.py"]
        assert extract_server_names(files) == {"deep"}


# ─── 11. Valid servers pass all checks (integration) ────────────────────


class TestValidServerIntegration:
    def test_valid_server_passes_everything(self, server_dir: Path):
        files = [
            "servers/test-server/server.py",
            "servers/test-server/requirements.txt",
        ]
        assert check_single_server(files) == []
        assert check_no_secrets(files) == []
        assert check_required_files("test-server") == []
        assert check_entrypoint_schema("test-server") == []
        assert check_requirements_txt("test-server") == []
        assert check_env_template("test-server") == []
        assert check_file_sizes(files) == []
        assert check_hardcoded_secrets(files) == []


# ─── Run standalone ─────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
