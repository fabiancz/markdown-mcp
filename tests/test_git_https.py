import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git

from obsidian_mcp.git_source import ManagedGit
from obsidian_mcp.models import DomainError
from obsidian_mcp.read import Reader

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from git_fixture import https_git


async def test_authenticated_https_clone_fetch_and_secret_scope(vault):
    settings, _, origin, _ = vault
    remote = origin.parent / "owner" / "vault.git"
    remote.parent.mkdir()
    subprocess.run(
        ["git", "clone", "--bare", str(origin), str(remote)], check=True, capture_output=True
    )
    with https_git(origin.parent) as (port, cert, records):
        settings.git_repo_url = f"https://localhost:{port}/owner/vault.git"
        settings.git_ca_bundle = cert
        source = ManagedGit(settings)
        reader = Reader(settings, source)
        result = await reader.search_notes("zaloha")
        assert result["data"]["items"][0]["path"] == "notes/Backup.md"
        assert any(record["authorized"] for record in records)
        config = (source.checkout / ".git" / "config").read_text()
        assert "test-only" not in config and settings.git_repo_url in config
        await reader.list_notes()
        assert source.fetch_count == 2 and reader.index.build_count == 1
        # A configured redirect never reaches the destination and never sends credentials there.
        redirected = settings.model_copy(
            update={
                "git_repo_url": f"https://localhost:{port}/redirect/vault.git",
                "repo_root": origin.parent / "redirect-repo",
                "data_root": origin.parent / "redirect-data",
            }
        )
        with pytest.raises(DomainError):
            ManagedGit(redirected).sync()
        assert not any(record["path"].startswith("/other/") for record in records)
        # Bad credentials fail without echoing token contents in a domain error.
        from pydantic import SecretStr

        wrong = settings.model_copy(
            update={
                "git_pat": SecretStr("synthetic-bad-secret"),
                "repo_root": origin.parent / "wrong-repo",
                "data_root": origin.parent / "wrong-data",
            }
        )
        with pytest.raises(DomainError) as error:
            ManagedGit(wrong).sync()
        assert "synthetic-bad-secret" not in str(error.value)
        assert not (wrong.repo_root / "checkout").exists()
    # Changing the configured source must never overwrite existing data.
    settings.git_repo_url = "https://different.example/owner/vault.git"
    with pytest.raises(DomainError) as error:
        ManagedGit(settings)
    assert error.value.code == "SOURCE_MISMATCH"
    assert git(source.checkout, "rev-parse", "HEAD")
