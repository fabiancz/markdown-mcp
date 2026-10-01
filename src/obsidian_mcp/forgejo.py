"""Forgejo review adapter with marker reconciliation and bounded HTTP requests."""

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
