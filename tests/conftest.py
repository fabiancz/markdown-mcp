import subprocess

import pytest

from obsidian_mcp.config import Settings
from obsidian_mcp.git_source import ManagedGit


def git(path, *args):
    if args[0] != "init":
        root = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"], text=True
        ).strip()
        assert root == str(path)
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL)


@pytest.fixture
def vault(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    git(origin, "config", "user.name", "Fixture")
    git(origin, "config", "user.email", "fixture@example.invalid")
    files = {
        "notes/Backup.md": (
            "---\n"
            "aliases: [záloha, backup guide]\n"
            "tags: [ops]\n"
            "active: true\n"
            "---\n"
            "# Backups\n"
            "Záloha databáze.\n"
            "## Restore\n"
            "Use docker compose at https://example.invalid/a?b=c\n"
            "IP 192.0.2.1 token foo:bar-baz\n"
            "^restore\n"
        ),
        "notes/Body.md": (
            "# Other\n"
            "A brief mention of záloha and backup.\n"
            "[[Backup#Restore]] ![[Backup#^restore]]\n"
            "[restore](Backup.md#Restore)\n"
            "#projekt/backend\n"
            "```js\n"
            "#ignored\n"
            "[[Hidden]]\n"
            "```\n"
        ),
        "noteworthy/Other.md": "# Other folder\nZáloha\n",
        "notes/Broken.md": "---\ninvalid: [\n---\n# Broken\nReadable despite invalid YAML\n",
        ".obsidian/Hidden.md": "# Secret\nsecret hidden\n[[Backup]]\n",
        "notes/Empty.md": "",
    }
    for path, text in files.items():
        file = origin / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
    (origin / "notes" / "Link.md").symlink_to("Backup.md")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "test: initial fixture")
    settings = Settings(
        deployment_mode="tunnel",
        git_provider="forgejo",
        git_repo_url="https://fixture.invalid/owner/vault.git",
        git_username="fixture",
        git_pat="test-only",
        repo_root=tmp_path / "repo",
        data_root=tmp_path / "data",
    )

    class LocalGit(ManagedGit):
        # Exercise real clone/fetch/tree/object commands, replacing only network transport.
        def run(self, args, **kwargs):
            rewrite = f"url.{origin}.insteadOf={settings.git_repo_url}"
            if args[:2] == ["remote", "get-url"]:
                args = ["config", "--get", "remote.origin.url"]
            return super().run(["-c", rewrite, *args], **kwargs)

    return settings, LocalGit(settings), origin, files
