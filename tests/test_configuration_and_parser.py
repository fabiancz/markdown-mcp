import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import git
from pydantic import SecretStr, ValidationError

from obsidian_mcp.config import Settings
from obsidian_mcp.parser import parse_note
from obsidian_mcp.read import Reader

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "provider,url,id,api",
    [
        ("github", "https://github.com/owner/vault.git", "owner/vault", "https://api.github.com"),
        (
            "gitlab",
            "https://gitlab.example/group/subgroup/vault.git",
            "group/subgroup/vault",
            "https://gitlab.example/api/v4",
        ),
        (
            "forgejo",
            "https://forge.example/owner/vault.git",
            "owner/vault",
            "https://forge.example/api/v1",
        ),
    ],
)
def test_provider_identity_and_api_defaults(vault, provider, url, id, api):
    settings = Settings(**dict(vault[0].model_dump(), git_provider=provider, git_repo_url=url))
    assert settings.repository_id == id and settings.api_url == api
    explicit = Settings(
        **dict(
            settings.model_dump(),
            forge_repo_id="owner/vault",
            forge_api_url="https://forge.example/subpath/api/v1",
        )
    )
    assert explicit.repository_id == "owner/vault" and explicit.api_url.endswith("subpath/api/v1")


def test_http_git_requires_opt_in_and_keeps_url_restrictions(vault):
    values = dict(vault[0].model_dump(), git_repo_url="http://127.0.0.1:3006/owner/vault.git")
    with pytest.raises(ValidationError, match="GIT_ALLOW_HTTP"):
        Settings(**values)
    settings = Settings(**dict(values, git_allow_http=True))
    assert settings.api_url == "http://127.0.0.1:3006/api/v1"
    assert (
        Settings(
            **dict(settings.model_dump(), forge_api_url="http://127.0.0.1:3006/api/v1")
        ).api_url
        == settings.api_url
    )
    with pytest.raises(ValidationError, match="GIT_ALLOW_HTTP"):
        Settings(**dict(vault[0].model_dump(), forge_api_url="http://127.0.0.1:3006/api/v1"))
    for url in [
        "ssh://git@127.0.0.1:226/owner/vault.git",
        "http://user:token@127.0.0.1:3006/owner/vault.git",
        "http://127.0.0.1:3006/owner/vault.git?token=secret",
        "http://127.0.0.1:3006/owner/vault.git#fragment",
    ]:
        with pytest.raises(ValidationError):
            Settings(**dict(settings.model_dump(), git_repo_url=url))
    # Git's opt-in must never permit a plain HTTP OAuth origin.
    with pytest.raises(ValidationError, match="OAuth requires an HTTPS"):
        Settings(
            **dict(
                settings.model_dump(),
                deployment_mode="oauth",
                public_base_url="http://mcp.example.com",
            )
        )


def test_http_credential_helper_scopes_protocol_host_port_and_path():
    env = dict(
        os.environ,
        VAULT_GIT_URL="http://127.0.0.1:3006/owner/vault.git",
        VAULT_GIT_ALLOW_HTTP="true",
        VAULT_GIT_USERNAME="fixture",
        VAULT_GIT_PAT="synthetic-http-token",
    )
    request = {"protocol": "http", "host": "127.0.0.1:3006", "path": "owner/vault.git"}

    def invoke(fields, environment=env):
        return subprocess.run(
            [sys.executable, str(ROOT / "src/obsidian_mcp/credential_helper.py"), "get"],
            env=environment,
            input="".join(f"{k}={v}\n" for k, v in fields.items()) + "\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    assert "password=synthetic-http-token" in invoke(request)
    for update in [
        {"protocol": "https"},
        {"host": "other.example:3006"},
        {"host": "127.0.0.1:3007"},
        {"path": "owner/other.git"},
    ]:
        assert invoke(dict(request, **update)) == ""
    assert invoke(request, dict(env, VAULT_GIT_ALLOW_HTTP="false")) == ""


def test_compose_examples_match_settings_and_isolate_secrets():
    for profile in ["oauth", "tunnel"]:
        compose = yaml.safe_load((ROOT / f"examples/{profile}/docker-compose.yaml").read_text())
        env = compose["services"]["mcp"]["environment"]
        assert set(k.lower() for k in env).issubset(Settings.model_fields)
        assert env["DEPLOYMENT_MODE"] == profile
        values = {}
        for line in (ROOT / f"examples/{profile}/.env.example").read_text().splitlines():
            if line and not line.startswith("#"):
                k, v = line.split("=", 1)
                values[k] = v
        parsed = Settings(
            **{k.lower(): values[k] for k in env if k in values}, deployment_mode=profile
        )
        assert parsed.git_repo_url.startswith("https://")
        mcp = compose["services"]["mcp"]
        assert mcp["image"] and "build" not in mcp
        assert mcp["user"] == "10001:10001" and mcp["read_only"]
        assert len(mcp["volumes"]) == 2 and all(not v.endswith(":ro") for v in mcp["volumes"])
        assert "OPENAI_TUNNEL_API_KEY" not in env
        if profile == "tunnel":
            tunnel = compose["services"]["tunnel"]
            assert "ports" not in mcp and "ports" not in tunnel
            assert "volumes" not in tunnel and "GIT_PAT" not in tunnel["environment"]
            assert tunnel["environment"]["MCP_SERVER_URL"] == "http://mcp:8000/mcp"
            assert "@sha256:" in values["TUNNEL_IMAGE"]
        else:
            assert mcp["ports"][0].startswith("127.0.0.1:")


def test_secret_file_conflict_and_cli_error_redaction(vault):
    settings = vault[0]
    secret = settings.repo_root.parent / "token"
    secret.write_text("synthetic-file-token\n")
    values = dict(settings.model_dump(), git_pat=SecretStr(""), git_pat_file=secret)
    assert Settings(**values).git_pat.get_secret_value() == "synthetic-file-token"
    with pytest.raises(ValidationError):
        Settings(**dict(values, git_pat="conflicting-secret"))
    env = dict(
        os.environ,
        DEPLOYMENT_MODE="oauth",
        GIT_PROVIDER="forgejo",
        GIT_REPO_URL="https://fixture.invalid/owner/vault.git",
        GIT_USERNAME="fixture",
        GIT_PAT="must-never-appear-in-output",
        DATA_ROOT=str(settings.data_root),
        REPO_ROOT=str(settings.repo_root),
    )
    result = subprocess.run(
        [sys.executable, "-m", "obsidian_mcp.server"], env=env, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert env["GIT_PAT"] not in result.stdout + result.stderr
    assert "Invalid configuration" in result.stderr


@pytest.mark.parametrize(
    "yaml_content",
    [
        "x: !!python/object/apply:os.system ['echo never-run']",
        "x: &node [*node]",
        "x: " + "[" * 20 + "0" + "]" * 20,
        "x: " + "a" * 17000,
    ],
)
def test_frontmatter_limits_preserve_raw_text(yaml_content):
    raw = ("---\n" + yaml_content + "\n---\n# Readable\ntext\n").encode()
    parsed = parse_note("Note.md", raw)
    assert parsed["text"].encode() == raw
    assert parsed["frontmatter"] == {} and parsed["warnings"] == ["INVALID_FRONTMATTER"]


async def test_file_diagnostics_and_path_collision_preserve_snapshot(vault):
    settings, source, origin, _ = vault
    reader = Reader(settings, source)
    before = await reader.snapshot()
    (origin / "Invalid.md").write_bytes(b"\xff\xfe")
    (origin / "Lfs.md").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64
    )
    (origin / "Large.md").write_text("x" * (settings.max_file_bytes + 1))
    git(origin, "add", ".")
    git(origin, "commit", "-m", "test: skipped source files")
    listed = await reader.list_notes(limit=50)
    assert {
        "INVALID_UTF8_SKIPPED",
        "LFS_POINTER_SKIPPED",
        "FILE_TOO_LARGE_SKIPPED",
        "NON_REGULAR_MARKDOWN_SKIPPED",
    }.issubset(listed["meta"]["warnings"])
    paths = [n["path"] for n in listed["data"]["items"]]
    assert not {"Invalid.md", "Lfs.md", "Large.md", "notes/Link.md"}.intersection(paths)
    stable = listed["meta"]["snapshot_id"]
    oid = git(origin, "rev-parse", "HEAD:notes/Broken.md").decode().strip()
    git(origin, "update-index", "--add", "--cacheinfo", "100644", oid, "notes/broken.md")
    git(origin, "commit", "-m", "test: colliding Git path")
    response = await reader.list_notes()
    assert response["meta"]["stale"] and reader.index.active() == stable
    assert any("PATH_COLLISION" in w for w in response["meta"]["warnings"])
    assert (await reader.read_note("notes/Backup.md", before.snapshot_id))["data"]["text"]


async def test_sql_failure_rolls_back_entire_generation(vault):
    settings, source, origin, _ = vault
    reader = Reader(settings, source)
    first = await reader.list_notes(limit=50)
    with reader.index.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_new_note BEFORE INSERT ON notes "
            "WHEN NEW.path='new.md' BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    (origin / "new.md").write_text("new generation")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "test: transactional publication failure")
    second = await reader.list_notes(limit=50)
    assert second["meta"]["stale"]
    assert second["meta"]["snapshot_id"] == first["meta"]["snapshot_id"]
    assert second["data"] == first["data"]
    with reader.index.connect() as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        assert (
            db.execute("SELECT count(*) FROM notes").fetchone()[0]
            == db.execute("SELECT count(*) FROM notes_fts").fetchone()[0]
        )
