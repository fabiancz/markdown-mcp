"""Managed Git source: reads objects, never changes the checked-out branch."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .config import Settings
from .models import DomainError
from .policy import Policy, path_key
from .repo_lock import file_lock


def atomic_json(path: Path, data: dict):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data))
    temporary.chmod(0o600)
    os.replace(temporary, path)


class ManagedGit:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.repo_root
        self.checkout = self.root / "checkout"
        self.policy = Policy(settings.allowed_folders, settings.excluded_folders)
        self.branch = settings.git_target_branch
        self.fetch_count = 0
        for root in (self.root, settings.data_root):
            root.mkdir(parents=True, exist_ok=True)
            test = root / (".write-" + uuid.uuid4().hex)
            try:
                test.write_bytes(b"")
                test.unlink()
            except OSError:
                raise DomainError(
                    "STORAGE_UNAVAILABLE", "Both persistent directories must be writable"
                ) from None
        identity = settings.data_root / "source.json"
        if identity.exists():
            saved = json.loads(identity.read_text())
            if saved.get("source") != settings.source_identity:
                raise DomainError(
                    "SOURCE_MISMATCH",
                    "Use a new pair of persistent directories for a different repository",
                )
            self.branch = self.branch or saved.get("branch", "")
        self.identity_file = identity

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        input: bytes | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> bytes:
        helper_path = Path(__file__).with_name("credential_helper.py")
        helper = f"{shlex.quote(sys.executable)} {shlex.quote(str(helper_path))}"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "VAULT_GIT_"))}
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "VAULT_GIT_URL": self.settings.git_repo_url,
                "VAULT_GIT_ALLOW_HTTP": str(self.settings.git_allow_http).lower(),
                "VAULT_GIT_USERNAME": self.settings.git_username,
                "VAULT_GIT_PAT": self.settings.git_pat.get_secret_value(),
            }
        )
        env.update(extra_env or {})
        command = [
            "git",
            "--literal-pathspecs",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.attributesFile=" + os.devnull,
            "-c",
            "submodule.recurse=false",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=" + helper,
            "-c",
            "credential.useHttpPath=true",
            "-c",
            "http.followRedirects=false",
            "-c",
            "core.hooksPath=" + os.devnull,
            *args,
        ]
        # Even `status` may run a repository-defined clean/process filter. Discover only
        # configuration keys, then override every filter without evaluating its command.
        if cwd is not None and (cwd / ".git").exists():
            try:
                configured = subprocess.run(
                    ["git", "config", "--includes", "--name-only", "--get-regexp", r"^filter\."],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    timeout=self.settings.git_timeout_seconds,
                )
                if configured.returncode not in {0, 1}:
                    raise OSError()
                names = {key.rsplit(".", 1)[0] for key in configured.stdout.decode().splitlines()}
                if len(names) > 1000:
                    raise OSError()
                for name in sorted(names):
                    for key, value in [
                        ("clean", ""),
                        ("smudge", ""),
                        ("process", ""),
                        ("required", "false"),
                    ]:
                        command[1:1] = ["-c", name + "." + key + "=" + value]
            except (OSError, UnicodeError, subprocess.TimeoutExpired):
                raise DomainError(
                    "SOURCE_MISMATCH", "Cannot safely inspect Git configuration"
                ) from None
        if self.settings.git_ca_bundle:
            command[1:1] = ["-c", "http.sslCAInfo=" + str(self.settings.git_ca_bundle)]
        try:
            process = subprocess.run(
                command,
                cwd=cwd,
                input=input,
                env=env,
                capture_output=True,
                timeout=self.settings.git_timeout_seconds,
            )
        except (subprocess.TimeoutExpired, OSError):
            raise DomainError(
                "SYNC_UNAVAILABLE", "Git operation timed out or could not run", True
            ) from None
        if process.returncode:
            # Git stderr can contain credentials, local paths and private repository names.
            raise DomainError(
                "SYNC_UNAVAILABLE",
                "Git operation failed; check remote access and branch configuration",
                True,
            )
        return process.stdout

    def initialize(self):
        if self.checkout.exists():
            if (
                self.checkout.is_symlink()
                or (self.checkout / ".git").is_symlink()
                or not (self.checkout / ".git").is_dir()
            ):
                raise DomainError(
                    "SOURCE_MISMATCH", "Existing checkout is not a managed repository"
                )
            origin = self.run(["remote", "get-url", "origin"], cwd=self.checkout).decode().strip()
            root = self.run(["rev-parse", "--show-toplevel"], cwd=self.checkout).decode().strip()
            if (
                origin != self.settings.git_repo_url
                or Path(root).resolve() != self.checkout.resolve()
            ):
                raise DomainError(
                    "SOURCE_MISMATCH", "Existing checkout does not match the configured repository"
                )
        else:
            stage = self.root / (".clone-" + uuid.uuid4().hex)
            try:
                args = ["clone", "--no-checkout", "--no-tags"]
                if self.branch:
                    args += ["--branch", self.branch]
                self.run([*args, "--", self.settings.git_repo_url, str(stage)])
                # Establish a clean, initial checkout once. Later reads only access commit objects.
                self.run(["checkout"], cwd=stage)
                os.rename(stage, self.checkout)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        if not self.branch:
            symbolic = (
                self.run(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=self.checkout)
                .decode()
                .strip()
            )
            self.branch = symbolic.removeprefix("refs/remotes/origin/")
        self.run(["check-ref-format", "refs/heads/" + self.branch], cwd=self.checkout)
        atomic_json(
            self.identity_file, {"source": self.settings.source_identity, "branch": self.branch}
        )

    def lock(self):
        return file_lock(self.root / ".repo.lock")

    def sync(self) -> tuple[str, list[tuple[str, bytes]], list[str]]:
        with self.lock():
            return self._sync()

    def _sync(self) -> tuple[str, list[tuple[str, bytes]], list[str]]:
        self.initialize()
        self.fetch_count += 1
        self.run(
            [
                "fetch",
                "--no-tags",
                "origin",
                f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}",
            ],
            cwd=self.checkout,
        )
        commit = (
            self.run(
                ["rev-parse", f"refs/remotes/origin/{self.branch}^{{commit}}"], cwd=self.checkout
            )
            .decode()
            .strip()
        )
        warnings = []
        if self.run(["status", "--porcelain", "-z"], cwd=self.checkout):
            warnings.append("DIRTY_CHECKOUT_IGNORED")
        return commit, [], warnings

    def files(self, commit: str) -> tuple[list[tuple[str, bytes]], list[str]]:
        tree = self.run(["ls-tree", "-rlz", commit], cwd=self.checkout)
        selected, warnings, normalized = [], [], set()
        total = 0
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, path_bytes = entry.split(b"\t", 1)
            try:
                path = path_bytes.decode("utf-8")
            except UnicodeError:
                warnings.append("NON_UTF8_PATH_SKIPPED")
                continue
            if not path.lower().endswith(".md") or not self.policy.permits(path):
                continue
            mode, kind, oid, size = metadata.split()
            if mode not in (b"100644", b"100755") or kind != b"blob":
                warnings.append("NON_REGULAR_MARKDOWN_SKIPPED")
                continue
            key = path_key(path)
            if key in normalized:
                raise DomainError(
                    "PATH_COLLISION", "Vault paths collide after Unicode/case normalization"
                )
            normalized.add(key)
            if int(size) > self.settings.max_file_bytes:
                warnings.append("FILE_TOO_LARGE_SKIPPED")
                continue
            total += int(size)
            if total > self.settings.max_index_bytes or len(selected) >= 100000:
                raise DomainError("INDEX_LIMIT", "The source exceeds the configured index limits")
            selected.append((path, oid, int(size)))
        if not selected:
            return [], warnings
        data = self.run(
            ["cat-file", "--batch"],
            cwd=self.checkout,
            input=b"".join(oid + b"\n" for _, oid, _ in selected),
        )
        offset, files = 0, []
        for path, oid, size in selected:
            end = data.index(b"\n", offset)
            header = data[offset:end].split()
            if header != [oid, b"blob", str(size).encode()]:
                raise DomainError(
                    "INDEX_FAILED", "Git object batch did not match the selected tree"
                )
            raw = data[end + 1 : end + 1 + size]
            offset = end + size + 2
            if raw.startswith(b"version https://git-lfs.github.com/spec/v1"):
                warnings.append("LFS_POINTER_SKIPPED")
                continue
            try:
                raw.decode("utf-8")
                if b"\0" in raw:
                    raise UnicodeError()
            except UnicodeError:
                warnings.append("INVALID_UTF8_SKIPPED")
                continue
            files.append((path, raw))
        return files, warnings
