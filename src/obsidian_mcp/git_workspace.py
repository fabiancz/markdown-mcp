"""Isolated drafts built with Git plumbing, without hooks, filters or merge drivers."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, ValidationError

from .git_source import ManagedGit
from .models import DomainError
from .parser import parse_note
from .policy import normalize, path_key, safe_path


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    op: str
    path: str
    content: str | None = None
    expected_revision: str | None = None
    destination: str | None = None


def canonical_operations(values: list[dict]) -> list[dict]:
    try:
        result = [Operation.model_validate(v).model_dump(exclude_none=True) for v in values]
    except ValidationError:
        raise DomainError("INVALID_OPERATION", "Invalid operation fields or types") from None
    for op in result:
        kind = op["op"]
        fields = {"op", "path"}
        if kind in {"create", "replace"}:
            fields.add("content")
        if kind in {"replace", "delete", "rename"}:
            fields.add("expected_revision")
        if kind == "rename":
            fields.add("destination")
        if kind not in {"create", "replace", "delete", "rename"} or set(op) != fields:
            raise DomainError(
                "INVALID_OPERATION", "Use create, replace, delete or rename with required fields"
            )
    # Non-overlapping operations commute; ordering does not change idempotency.
    return sorted(result, key=lambda op: (op["path"], op["op"]))


class DraftWorkspace:
    def __init__(self, source: ManagedGit):
        self.source = source
        self.settings = source.settings
        self.root = source.root / "worktrees"
        self.root.mkdir(exist_ok=True)

    def run(self, args, **kwargs):
        return self.source.run(args, cwd=kwargs.pop("cwd", self.source.checkout), **kwargs)

    def tree(self, commit: str) -> dict[str, tuple[str, str]]:
        result = {}
        for entry in self.run(["ls-tree", "-rz", commit]).split(b"\0"):
            if entry:
                meta, path = entry.split(b"\t", 1)
                mode, kind, oid = meta.decode().split()
                try:
                    result[path.decode("utf-8")] = (mode, oid)
                except UnicodeError:
                    raise DomainError(
                        "INVALID_PATH", "Write requires UTF-8 repository paths"
                    ) from None
        return result

    def path(self, path: str, tree: dict, *, existing: bool):
        safe_path(path)
        if (
            not path.lower().endswith(".md")
            or not self.source.policy.permits(path)
            or any(path_key(part) in {".git", ".obsidian", ".trash"} for part in path.split("/"))
            or path.endswith("/")
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or len(path.encode()) > 1024
        ):
            raise DomainError(
                "INVALID_PATH", "Only permitted regular Markdown paths can be changed"
            )
        for p in PurePosixPath(path).parents:
            if str(p) != "." and any(path_key(str(p)) == path_key(t) for t in tree):
                raise DomainError("INVALID_PATH", "A destination ancestor is not a directory")
        if existing:
            if path not in tree or tree[path][0] not in {"100644", "100755"}:
                raise DomainError("NOTE_NOT_FOUND", "The source is not a regular Markdown note")
        elif any(path_key(p) == path_key(path) or p.startswith(path + "/") for p in tree):
            raise DomainError(
                "PATH_COLLISION", "Destination already exists or collides with a vault path"
            )

    def build_tree(self, base: str, changes: dict[str, tuple[str, str] | None]) -> str:
        # The private index cannot stage anything from the service checkout or a worktree.
        fd, index = tempfile.mkstemp(prefix=".index-", dir=self.root)
        os.close(fd)
        Path(index).unlink()
        env = {"GIT_INDEX_FILE": index}
        try:
            self.run(["read-tree", base], extra_env=env)
            records = []
            for path, value in sorted(changes.items()):
                mode, oid = value or ("0", "0" * 40)
                records.append(f"{mode} {oid}\t{path}".encode() + b"\0")
            self.run(["update-index", "-z", "--index-info"], input=b"".join(records), extra_env=env)
            return self.run(["write-tree"], extra_env=env).decode().strip()
        finally:
            for suffix in ("", ".lock"):
                Path(index + suffix).unlink(missing_ok=True)

    def diff(self, base: str, tree: str) -> bytes:
        return self.run(
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--binary",
                "--full-index",
                base,
                tree,
                "--",
            ]
        )

    def prepare(self, base: str, operations: list[dict]) -> dict:
        tree = self.tree(base)
        keys = [path_key(p) for p in tree]
        if len(keys) != len(set(keys)):
            raise DomainError("PATH_COLLISION", "Repository paths collide after normalization")
        paths, changes, originals, contents = set(), {}, {}, {}
        for op in operations:
            path, kind = op["path"], op["op"]
            used = [path] + ([op["destination"]] if kind == "rename" else [])
            for p in used:
                if any(path_key(p) == path_key(other) for other in paths):
                    raise DomainError("INVALID_OPERATION", "Operations must use distinct paths")
                paths.add(p)
            self.path(path, tree, existing=kind != "create")
            old = None
            if kind != "create":
                old = self.run(["cat-file", "blob", tree[path][1]])
                if hashlib.sha256(old).hexdigest() != op["expected_revision"]:
                    raise DomainError(
                        "REVISION_MISMATCH", "Revision does not match the base snapshot"
                    )
                if len(old) > self.settings.max_file_bytes:
                    raise DomainError("RESPONSE_LIMIT", "Source note exceeds the file limit")
                try:
                    old.decode("utf-8")
                except UnicodeError:
                    raise DomainError("INVALID_CONTENT", "Source note must be UTF-8") from None
            originals[path] = tree.get(path)
            if kind in {"create", "replace"}:
                try:
                    raw = op["content"].encode("utf-8")
                except UnicodeError:
                    raise DomainError("INVALID_CONTENT", "Markdown must be valid UTF-8") from None
                if len(raw) > self.settings.max_file_bytes or b"\0" in raw:
                    raise DomainError(
                        "INVALID_CONTENT", "Markdown exceeds the limit or contains NUL"
                    )
                if re.search(rb"(?m)^(<<<<<<< |=======\r?$|>>>>>>> )", raw):
                    raise DomainError(
                        "INVALID_CONTENT", "Resolve conflict markers before preparing a note"
                    )
                if parse_note(path, raw)["warnings"]:
                    raise DomainError(
                        "INVALID_CONTENT", "New Markdown must have valid bounded frontmatter"
                    )
                oid = self.run(["hash-object", "-w", "--stdin"], input=raw).decode().strip()
                changes[path] = (tree.get(path, ("100644", ""))[0], oid)
                contents[path] = op["content"]
            else:
                changes[path] = None
            if kind == "rename":
                dest = op["destination"]
                self.path(dest, tree, existing=False)
                originals[dest] = None
                changes[dest] = tree[path]
                contents[dest] = old.decode("utf-8")
        if any(p.startswith(other + "/") for p in paths for other in paths if p != other):
            raise DomainError("PATH_COLLISION", "Changed paths overlap as files and directories")
        desired = self.build_tree(base, changes)
        raw_diff = self.diff(base, desired)
        if len(raw_diff) > self.settings.write_max_bytes:
            raise DomainError("RESPONSE_LIMIT", "Prepared diff exceeds the write limit")
        return {
            "paths": sorted(paths),
            "originals": originals,
            "result_blobs": changes,
            "contents": contents,
            "tree": desired,
            "diff": raw_diff.decode("utf-8"),
            "diff_hash": hashlib.sha256(raw_diff).hexdigest(),
        }

    @staticmethod
    def branch(summary: str, id: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", normalize(summary)).strip("-")[:48] or "change"
        return "mcp/" + slug + "-" + id.removeprefix("chg_")

    def local_ref(self, branch: str) -> str | None:
        result = self.run(["for-each-ref", "--format=%(objectname)", "refs/heads/" + branch])
        return result.decode().strip() or None

    def commit(self, change: dict) -> str:
        desired = self.build_tree(change["base_commit"], change["result_blobs"])
        if (
            desired != change["tree"]
            or hashlib.sha256(self.diff(change["base_commit"], desired)).hexdigest()
            != change["diff_hash"]
        ):
            raise DomainError("DIFF_MISMATCH", "Stored draft no longer matches the approved diff")
        env = {
            "GIT_AUTHOR_NAME": change["commit_name"],
            "GIT_AUTHOR_EMAIL": change["commit_email"],
            "GIT_COMMITTER_NAME": change["commit_name"],
            "GIT_COMMITTER_EMAIL": change["commit_email"],
            "GIT_AUTHOR_DATE": change["commit_date"],
            "GIT_COMMITTER_DATE": change["commit_date"],
        }
        message = (
            change["summary"]
            + "\n\nMCP-Change-ID: "
            + change["change_id"]
            + "\nMCP-Actor-ID: "
            + change["actor_id"]
            + "\n"
        )
        head = (
            self.run(
                ["commit-tree", desired, "-p", change["base_commit"]],
                input=message.encode(),
                extra_env=env,
            )
            .decode()
            .strip()
        )
        ref = self.local_ref(change["branch"])
        if ref and ref != head:
            raise DomainError(
                "BRANCH_CHANGED", "Local change branch was modified; it will not be overwritten"
            )
        if not ref:
            self.run(["update-ref", "refs/heads/" + change["branch"], head, "0" * 40])
        self.ensure_worktree(change, head)
        return head

    def ensure_worktree(self, change: dict, head: str):
        path = self.root / change["change_id"]
        if path.is_symlink():
            raise DomainError("INVALID_PATH", "Draft worktree cannot be a symlink")
        if not path.exists():
            self.run(["worktree", "add", "--detach", "--no-checkout", str(path), head])
        root = self.run(["rev-parse", "--show-toplevel"], cwd=path).decode().strip()
        actual = self.run(["rev-parse", "HEAD"], cwd=path).decode().strip()
        if Path(root).resolve() != path.resolve() or actual != head:
            raise DomainError(
                "BRANCH_CHANGED", "Draft worktree was changed; recovery will not reset it"
            )
        for file in path.rglob("*"):
            if file.name == ".git" and file.parent == path:
                continue
            relative = file.relative_to(path).as_posix()
            if file.is_symlink() or (not file.is_dir() and relative not in change["contents"]):
                raise DomainError(
                    "BRANCH_CHANGED", "Unexpected worktree content will not be overwritten"
                )
            if file.is_file() and file.read_bytes() != change["contents"][relative].encode():
                raise DomainError("BRANCH_CHANGED", "Worktree edits will not be overwritten")
        # Files are a raw view of affected results. Unaffected objects stay in Git's tree.
        for relative, text in change["contents"].items():
            file = path / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(text.encode())
        marker = self.root / (change["change_id"] + ".json")
        marker.write_text(json.dumps({"change_id": change["change_id"], "head": head}))

    def remote_head(self, branch: str) -> str | None:
        result = self.run(["ls-remote", "--refs", "origin", "refs/heads/" + branch])
        return result.decode().split()[0] if result else None

    def publish(self, change: dict):
        existing = self.remote_head(change["branch"])
        if existing == change["head_commit"]:
            return
        if existing is not None:
            raise DomainError(
                "BRANCH_CHANGED", "Remote branch was changed; it will not be overwritten"
            )
        # Empty expected value atomically guards initial branch creation. This never rewrites a ref.
        self.run(
            [
                "push",
                "--porcelain",
                "--force-with-lease=refs/heads/" + change["branch"] + ":",
                "origin",
                change["head_commit"] + ":refs/heads/" + change["branch"],
            ]
        )
