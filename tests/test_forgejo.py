import json
from pathlib import Path

import httpx
import pytest

from obsidian_mcp.forgejo import ForgejoAdapter
from obsidian_mcp.models import DomainError

SCHEMA = json.loads((Path(__file__).parent / "fixtures/forgejo-15.0.9-review.json").read_text())


def change():
    return {
        "change_id": "chg_contract",
        "branch": "mcp/contract",
        "target_branch": "main",
        "summary": "Update notes",
        "actor_id": "test-actor",
        "paths": ["Note.md"],
        "base_commit": "a" * 40,
        "diff_hash": "b" * 64,
        "warnings": [],
    }


def pr(c, **updates):
    return dict(
        {
            "number": 1,
            "html_url": "https://fixture.invalid/owner/vault/pulls/1",
            "head": {"ref": c["branch"], "sha": "c" * 40, "repo_id": 9},
            "base": {"ref": "main", "repo_id": 9},
            "body": ForgejoAdapter.marker(c),
            "merged": False,
            "state": "open",
        },
        **updates,
    )


async def test_contract_timeout_pagination_closed_discovery_and_metadata(vault):
    settings = vault[0]
    rows, requests = [], []
    c = change()
    time_out = True

    def handler(request):
        nonlocal time_out
        requests.append(request)
        assert request.headers["Authorization"] == "token test-only"
        path = request.url.path
        if path == "/api/v1/version":
            return httpx.Response(200, json={"version": SCHEMA["source_version"]})
        if path == "/api/v1/repos/owner/vault":
            return httpx.Response(200, json={"id": 9, "full_name": "owner/vault"})
        if path == "/swagger.v1.json":
            return httpx.Response(200, json=SCHEMA)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert {"head", "base", "title", "body"} == set(payload)
            assert ForgejoAdapter.marker(c) in payload["body"]
            rows.append(pr(c))
            if time_out:
                time_out = False
                raise httpx.ReadTimeout("synthetic-token-must-not-leak", request=request)
        if path.endswith("/pulls"):
            # Server caps pages below requested limit; discovery must still visit page 2.
            page = int(request.url.params.get("page", 1))
            return httpx.Response(200, json=rows[page - 1 : page])
        return httpx.Response(200, json=rows[0])

    adapter = ForgejoAdapter(settings, httpx.MockTransport(handler))
    with pytest.raises(DomainError) as error:
        await adapter.create_or_find_cr(c)
    assert error.value.retryable and "synthetic-token" not in str(error.value)
    actual = await adapter.create_or_find_cr(c)
    assert actual["number"] == 1 and len(rows) == 1
    assert sum(r.method == "POST" for r in requests) == 1
    rows[0].update(state="closed", merged=True, merge_commit_sha="d" * 40)
    assert (await adapter.create_or_find_cr(c))["merged"]
    assert len(rows) == 1
    assert adapter.capabilities()["atomic_head_merge"] is None


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "PERMISSION_DENIED", False),
        (403, "PERMISSION_DENIED", False),
        (302, "PROVIDER_REJECTED", False),
        (422, "PROVIDER_REJECTED", False),
        (429, "PROVIDER_UNAVAILABLE", True),
        (503, "PROVIDER_UNAVAILABLE", True),
    ],
)
async def test_provider_error_mapping_and_no_redirect(vault, status, code, retryable):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://other.invalid/secret"},
            json={"message": "test-only must not leak"},
        )

    adapter = ForgejoAdapter(vault[0], httpx.MockTransport(handler))
    with pytest.raises(DomainError) as error:
        await adapter.request("POST", "/test", json={})
    assert error.value.code == code and error.value.retryable == retryable
    assert "test-only" not in str(error.value) and len(requests) == 1


@pytest.mark.parametrize(
    "update",
    [
        {"body": "removed marker"},
        {"head": {"ref": "mcp/contract", "repo_id": 99, "sha": "x"}},
        {"base": {"ref": "other", "repo_id": 9}},
        {"merged": "false"},
        {"merged": True},
    ],
)
async def test_foreign_or_ambiguous_pr_never_creates_duplicate(vault, update):
    c = change()
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
        if request.url.params.get("page") == "1":
            return httpx.Response(200, json=[pr(c, **update)])
        return httpx.Response(200, json=[])

    adapter = ForgejoAdapter(vault[0], httpx.MockTransport(handler))
    adapter.evidence = {"version": SCHEMA["source_version"], "repository_id": 9}
    with pytest.raises(DomainError) as error:
        await adapter.create_or_find_cr(c)
    assert error.value.code == "BRANCH_CHANGED" and not posts
