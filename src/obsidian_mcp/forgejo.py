"""Forgejo review adapter with marker reconciliation and bounded HTTP requests."""

import json
import ssl
from urllib.parse import quote, urlsplit

import httpx

from .config import Settings
from .models import DomainError


class ForgejoAdapter:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self.repository = settings.repository_id
        self.path = "/repos/" + "/".join(quote(p, safe="") for p in self.repository.split("/"))
        self.evidence: dict = {}

    def capabilities(self) -> dict[str, bool | None]:
        return {
            "review": True if self.evidence else None,
            "automatic_merge": False,
            "atomic_head_merge": None,
            "self_approval": None,
        }

    async def request(self, method: str, path: str, **kwargs):
        verify = ssl.create_default_context(
            cafile=str(self.settings.git_ca_bundle) if self.settings.git_ca_bundle else None
        )
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                verify=verify,
                follow_redirects=False,
                trust_env=False,
                timeout=self.settings.forge_timeout_seconds,
                headers={"Authorization": "token " + self.settings.git_pat.get_secret_value()},
            ) as client:
                async with client.stream(
                    method, self.settings.api_url + path, **kwargs
                ) as response:
                    if response.status_code in {401, 403}:
                        raise DomainError("PERMISSION_DENIED", "Forgejo denied repository access")
                    if response.status_code in {408, 429} or response.status_code >= 500:
                        raise DomainError(
                            "PROVIDER_UNAVAILABLE", "Forgejo is temporarily unavailable", True
                        )
                    if 300 <= response.status_code < 400:
                        raise DomainError("PROVIDER_REJECTED", "Forgejo redirects are not followed")
                    if response.status_code >= 400:
                        raise DomainError(
                            "PROVIDER_REJECTED", "Forgejo rejected the review operation"
                        )
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 8 * 1024 * 1024:
                            raise DomainError(
                                "PROVIDER_REJECTED", "Forgejo response exceeds the limit"
                            )
                    import json

                    return json.loads(data)
        except (httpx.HTTPError, OSError):
            # A timed-out POST may have succeeded; retry always starts with discovery.
            raise DomainError(
                "PROVIDER_UNAVAILABLE", "Forgejo operation has an unknown outcome", True
            ) from None
        except (ValueError, UnicodeError):
            raise DomainError(
                "PROVIDER_UNAVAILABLE", "Forgejo returned an invalid response", True
            ) from None

    async def verify(self) -> dict:
        if self.evidence:
            return self.evidence
        version = await self.request("GET", "/version")
        repo = await self.request("GET", self.path)
        # Swagger lives above /api/v1, also for installations hosted in a subpath.
        schema = await self.request("GET", "/../../swagger.v1.json")
        try:
            paths = schema["paths"]
            definitions = schema["definitions"]
            for endpoint, methods in {
                "/repos/{owner}/{repo}/pulls": ["get", "post"],
                "/repos/{owner}/{repo}/pulls/{index}": ["get", "patch"],
            }.items():
                if not all(m in paths[endpoint] for m in methods):
                    raise ValueError()
            required = {
                "CreatePullRequestOption": {"head", "base", "title", "body"},
                "PullRequest": {"number", "html_url", "head", "base", "body", "merged", "state"},
                "PRBranchInfo": {"ref", "sha", "repo_id"},
            }
            for name, fields in required.items():
                if not fields.issubset(definitions[name]["properties"]):
                    raise ValueError()
            if repo["full_name"].casefold() != self.repository.casefold():
                raise ValueError()
            self.evidence = {
                "version": version["version"],
                "repository_id": repo["id"],
                "review_schema_verified": True,
            }
            return self.evidence
        except (KeyError, TypeError, ValueError):
            raise DomainError(
                "PROVIDER_REJECTED", "Forgejo review schema or repository does not match"
            ) from None

    @staticmethod
    def marker(change: dict) -> str:
        return "<!-- markdown-mcp:" + change["change_id"] + " -->"

    def validate(self, pr: dict, change: dict) -> dict:
        try:
            if (
                pr["head"]["ref"] != change["branch"]
                or pr["base"]["ref"] != change["target_branch"]
                or pr["head"]["repo_id"] != self.evidence["repository_id"]
                or pr["base"]["repo_id"] != self.evidence["repository_id"]
                or self.marker(change) not in pr["body"]
            ):
                raise ValueError()
            url = urlsplit(pr["html_url"])
            if url.scheme not in {"https", "http"} or url.username or url.password:
                raise ValueError()
            if type(pr["merged"]) is not bool or pr["state"] not in {"open", "closed"}:
                raise ValueError()
            if type(pr["number"]) is not int or not isinstance(pr["head"]["sha"], str):
                raise ValueError()
            if pr["merged"] and not pr.get("merge_commit_sha"):
                raise ValueError()
            return pr
        except (KeyError, TypeError, ValueError):
            raise DomainError(
                "BRANCH_CHANGED", "PR identity or durable change marker does not match"
            ) from None

    async def find(self, change: dict) -> dict | None:
        found = None
        for page in range(1, 1001):
            rows = await self.request(
                "GET", self.path + "/pulls", params={"state": "all", "page": page, "limit": 50}
            )
            if not isinstance(rows, list):
                raise DomainError(
                    "PROVIDER_UNAVAILABLE", "Invalid Forgejo discovery response", True
                )
            if not rows:
                return found
            for pr in rows:
                if not isinstance(pr, dict) or not isinstance(pr.get("head"), dict):
                    raise DomainError("PROVIDER_UNAVAILABLE", "Invalid Forgejo PR record", True)
                if pr["head"].get("ref") == change["branch"]:
                    self.validate(pr, change)
                    if found is not None:
                        raise DomainError("BRANCH_CHANGED", "Multiple PRs match this change branch")
                    found = pr
        raise DomainError(
            "PROVIDER_REJECTED", "PR discovery limit reached; creation was not attempted"
        )

    async def create_or_find_cr(self, change: dict) -> dict:
        await self.verify()
        found = await self.find(change)
        if found:
            return found
        body = (
            self.marker(change)
            + "\n\nPrepared Markdown change for review.\n\n"
            + "Initiator: `"
            + change["actor_id"]
            + "`\n\n"
            + "Paths:\n"
            + "\n".join("- `" + p.replace("`", "\\`") + "`" for p in change["paths"])
            + "\n\nValidated UTF-8, paths, revisions and the approved diff.\n"
            + "Base: `"
            + change["base_commit"]
            + "`\n"
            + "Diff SHA-256: `"
            + change["diff_hash"]
            + "`\n"
            + "\n".join(change["warnings"])
        )
        try:
            result = await self.request(
                "POST",
                self.path + "/pulls",
                json={
                    "head": change["branch"],
                    "base": change["target_branch"],
                    "title": change["summary"],
                    "body": body,
                },
            )
        except DomainError as error:
            if error.code == "PROVIDER_REJECTED":
                found = await self.find(change)
                if found:
                    return found
            raise
        return self.validate(result, change)

    async def get_cr(self, number: int) -> dict:
        await self.verify()
        return await self.request("GET", self.path + f"/pulls/{number}")

    async def review_comments(self, number: int) -> list[dict]:
        await self.verify()
        if not self.evidence.get("review_comments_verified"):
            schema = await self.request("GET", "/../../swagger.v1.json")
            required = [
                "/repos/{owner}/{repo}/issues/{index}/comments",
                "/repos/{owner}/{repo}/pulls/{index}/reviews",
                "/repos/{owner}/{repo}/pulls/{index}/reviews/{id}/comments",
            ]
            if any("get" not in schema.get("paths", {}).get(path, {}) for path in required):
                raise DomainError("PROVIDER_REJECTED", "Forgejo review comment API is unsupported")
            self.evidence["review_comments_verified"] = True
        total_bytes, total_items = 0, 0

        async def pages(path, *, paginated=False):
            nonlocal total_bytes, total_items
            result = []
            for page in range(1, 101):
                # Forgejo paginates reviews, but returns discussion and inline
                # comments in one response (page/limit would be ignored).
                kwargs = {"params": {"page": page, "limit": 50}} if paginated else {}
                rows = await self.request("GET", path, **kwargs)
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise DomainError("PROVIDER_UNAVAILABLE", "Invalid review response", True)
                if not rows:
                    return result
                total_bytes += len(json.dumps(rows).encode())
                total_items += len(rows)
                if total_bytes > 8 * 1024 * 1024 or total_items > 5000:
                    raise DomainError("RESPONSE_LIMIT", "Review exceeds the retrieval limit")
                result.extend(rows)
                if not paginated:
                    return result
            raise DomainError("RESPONSE_LIMIT", "Review pagination exceeds the retrieval limit")

        def normalize(row, kind, review_id=None):
            if type(row.get("id")) is not int or not isinstance(row.get("body"), str):
                raise DomainError("PROVIDER_UNAVAILABLE", "Invalid review comment", True)
            author = row.get("user") or {}
            resolver = row.get("resolver") or {}
            if not isinstance(author, dict) or not isinstance(resolver, dict):
                raise DomainError("PROVIDER_UNAVAILABLE", "Invalid review author", True)
            for field in (
                "created_at",
                "submitted_at",
                "updated_at",
                "html_url",
                "state",
                "commit_id",
                "original_commit_id",
                "path",
                "diff_hunk",
            ):
                if row.get(field) is not None and not isinstance(row[field], str):
                    raise DomainError("PROVIDER_UNAVAILABLE", "Invalid review metadata", True)
            for field in ("position", "original_position"):
                if row.get(field) is not None and type(row[field]) is not int:
                    raise DomainError("PROVIDER_UNAVAILABLE", "Invalid review position", True)
            return {
                "kind": kind,
                "id": row["id"],
                "review_id": review_id,
                "body": row["body"],
                "author": {"id": author.get("id"), "login": author.get("login")},
                "created_at": row.get("created_at") or row.get("submitted_at"),
                "updated_at": row.get("updated_at"),
                "url": row.get("html_url"),
                "state": row.get("state"),
                "commit_sha": row.get("commit_id"),
                "original_commit_sha": row.get("original_commit_id"),
                "path": row.get("path"),
                "line": row.get("position"),
                "original_line": row.get("original_position"),
                "diff_hunk": row.get("diff_hunk"),
                "dismissed": row.get("dismissed"),
                "stale": row.get("stale"),
                "resolver": (
                    {"id": resolver.get("id"), "login": resolver.get("login")} if resolver else None
                ),
            }

        items = [
            normalize(row, "discussion")
            for row in await pages(self.path + f"/issues/{number}/comments")
        ]
        review_path = self.path + f"/pulls/{number}/reviews"
        for review in await pages(review_path, paginated=True):
            items.append(normalize(review, "review"))
            for comment in await pages(review_path + f"/{review['id']}/comments"):
                items.append(normalize(comment, "inline", review["id"]))
        return sorted(items, key=lambda item: (item["created_at"] or "", item["kind"], item["id"]))
