import base64
import hashlib
import re
import tomllib
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastmcp import Client
from pydantic import ValidationError
from starlette.testclient import TestClient

from obsidian_mcp.auth import make_auth, principal
from obsidian_mcp.config import Settings
from obsidian_mcp.read import Reader
from obsidian_mcp.server import create_server


def oauth_settings(settings):
    return settings.model_copy(
        update={
            "deployment_mode": "oauth",
            "public_base_url": "https://mcp.example.com",
            "github_oauth_client_id": "fixture-client",
            "github_allowed_user_ids": "42",
            "oauth_redirect_uris": "https://client.example/callback",
            "github_oauth_client_secret": settings.git_pat,
        }
    )


@pytest.mark.parametrize("write_enabled", [False, True])
async def test_mcp_release_version_and_write_discovery(vault, write_enabled):
    settings, source, _, _ = vault
    settings = settings.model_copy(update={"write_enabled": write_enabled})
    metadata = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    release_version = metadata["project"]["version"]
    assert version("obsidian-read-mcp") == release_version
    server = create_server(settings, Reader(settings, source))
    async with Client(server) as client:
        assert client.initialize_result.serverInfo.version == release_version
        icons = client.initialize_result.serverInfo.icons
        assert icons and len(icons) == 1
        assert icons[0].mimeType == "image/png" and icons[0].sizes == ["128x128"]
        prefix, encoded = icons[0].src.split(",", 1)
        assert prefix == "data:image/png;base64"
        assert base64.b64decode(encoded, validate=True) == (
            files("obsidian_mcp").joinpath("assets/icon.png").read_bytes()
        )
        fetches = source.fetch_count
        ping = (await client.call_tool("ping", {})).data["data"]
        assert ping == {
            "status": "ok",
            "mode": "tunnel",
            "version": release_version,
            "write_enabled": write_enabled,
            "write_default_mode": "review",
        }
        assert source.fetch_count == fetches
        names = {tool.name for tool in await client.list_tools()}
        write_tools = {
            "upload_attachment",
            "prepare_change",
            "submit_change",
            "get_change",
            "list_changes",
            "cancel_change",
            "get_change_review",
            "read_change_note",
            "prepare_change_update",
            "submit_change_update",
        }
        assert len(names) == (17 if write_enabled else 7)
        assert names & write_tools == (write_tools if write_enabled else set())


async def test_mcp_tools_annotations_and_error_signal(vault):
    settings, source, _, _ = vault
    server = create_server(settings, Reader(settings, source))
    async with Client(server) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools} == {
            "search_notes",
            "list_notes",
            "read_note",
            "get_note_outline",
            "get_backlinks",
            "get_vault_status",
            "ping",
        }
        assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)
        hits = (await client.call_tool("search_notes", {"query": "zaloha"})).data
        note = (
            await client.call_tool(
                "read_note",
                {
                    "path": hits["data"]["items"][0]["path"],
                    "snapshot_id": hits["meta"]["snapshot_id"],
                },
            )
        ).data
        assert "Záloha" in note["data"]["text"]
        bad = await client.call_tool("read_note", {"path": "../secret"}, raise_on_error=False)
        assert bad.is_error and "INVALID_PATH" in bad.content[0].text
    assert principal(settings).subject == "tunnel_operator"


def test_missing_auth_fails_closed_and_configuration(vault):
    settings, source, _, _ = vault
    values = settings.model_dump()
    for update in [
        {"deployment_mode": "oauth"},
        {"git_repo_url": "https://user:token@example.com/a/b"},
        {"write_enabled": True, "git_provider": "github"},
        {"yolo_enabled": True},
        {"git_target_branch": "--evil"},
        {"git_pat_file": settings.data_root / "missing"},
    ]:
        with pytest.raises((ValidationError, OSError)):
            Settings(**dict(values, **update))
    with pytest.raises(ValidationError):
        Settings(**dict(values, deployment_mode="oauth", public_base_url="http://mcp.example.com"))
    oauth = oauth_settings(settings)
    server = create_server(oauth, Reader(oauth, source))
    with TestClient(server.http_app(stateless_http=True), base_url=oauth.public_base_url) as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert metadata["issuer"] == oauth.public_base_url + "/"
        assert "S256" in metadata["code_challenge_methods_supported"]
        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
        for headers in [{}, {"Authorization": "Bearer invalid"}]:
            response = client.post(
                "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            assert response.status_code == 401
            assert "Backup" not in response.text
        fetches = source.fetch_count
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/health/ready").status_code == 200
        assert source.fetch_count == fetches
        assert (
            client.post(
                "/register", json={"redirect_uris": ["https://evil.example/callback"]}
            ).status_code
            == 400
        )


@pytest.mark.parametrize("has_refresh", [False, True])
def test_oauth_pkce_consent_allowlist_restart_refresh_and_revocation(
    vault, monkeypatch, has_refresh
):
    settings, source, _, _ = vault
    settings = oauth_settings(settings)
    user = {"id": 42, "revoked": False}
    original_send = httpx.AsyncClient.send

    async def upstream(client, request, *args, **kwargs):
        if request.url.host == "api.github.com":
            if user["revoked"]:
                return httpx.Response(401, request=request)
            if request.url.path == "/user":
                return httpx.Response(
                    200, json={"id": user["id"], "login": "fixture"}, request=request
                )
            return httpx.Response(
                200, headers={"X-OAuth-Scopes": "read:user"}, json=[], request=request
            )
        if request.url.host == "github.com" and request.url.path.endswith("access_token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-upstream-secret",
                    "token_type": "bearer",
                    "scope": "read:user",
                    **(
                        {"refresh_token": "synthetic-refresh-secret", "expires_in": 3600}
                        if has_refresh
                        else {}
                    ),
                },
                request=request,
            )
        return await original_send(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", upstream)
    server = create_server(settings, Reader(settings, source))
    verifier = "a" * 43
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    with TestClient(
        server.http_app(stateless_http=True),
        base_url=settings.public_base_url,
        follow_redirects=False,
    ) as client:
        registration = client.post(
            "/register",
            json={
                "redirect_uris": settings.redirect_uris,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "scope": "read:user",
            },
        )
        assert registration.status_code == 201, registration.text
        client_id = registration.json()["client_id"]
        params = {
            "client_id": client_id,
            "redirect_uri": settings.redirect_uris[0],
            "response_type": "code",
            "scope": "read:user",
            "state": "fixture-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": settings.public_base_url + "/mcp",
        }
        auth = client.get("/authorize", params=params)
        assert auth.status_code in (302, 307), auth.text
        consent = client.get(auth.headers["location"])
        assert consent.status_code == 200
        fields = dict(re.findall(r'name="([^\"]+)" value="([^\"]*)"', consent.text))
        approved = client.post(auth.headers["location"], data=dict(fields, action="approve"))
        assert approved.status_code == 302, approved.text
        state = parse_qs(urlsplit(approved.headers["location"]).query)["state"][0]
        callback = client.get(
            "/auth/callback", params={"state": state, "code": "synthetic-idp-code"}
        )
        assert callback.status_code == 302, callback.text
        code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
        token_params = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": settings.redirect_uris[0],
            "code_verifier": verifier,
            "resource": settings.public_base_url + "/mcp",
        }
        wrong = client.post("/token", data=dict(token_params, code_verifier="b" * 43))
        assert wrong.status_code in (400, 401), wrong.text
        tokens = client.post("/token", data=token_params)
        assert tokens.status_code == 200, tokens.text
        tokens = tokens.json()
        assert client.post("/token", data=token_params).status_code in (400, 401)

    # Public verifier checks the durable token mappings and signing key after reconstruction.
    async def verify_after_restart():
        provider = make_auth(settings)
        provider.set_mcp_path("/mcp")
        token = await provider.verify_token(tokens["access_token"])
        assert token.claims["sub"] == "42"
        other_resource = settings.model_copy(
            update={"public_base_url": "https://different.example"}
        )
        other_provider = make_auth(other_resource)
        other_provider.set_mcp_path("/mcp")
        assert await other_provider.verify_token(tokens["access_token"]) is None
        user["id"] = 43
        assert await provider.verify_token(tokens["access_token"]) is None
        user["id"] = 42
        provider.allowed_user_ids = frozenset()
        assert await provider.verify_token(tokens["access_token"]) is None
        provider.allowed_user_ids = frozenset({"42"})
        user["revoked"] = True
        assert await provider.verify_token(tokens["access_token"]) is None
        user["revoked"] = False

    import asyncio

    asyncio.run(verify_after_restart())
    restarted = create_server(settings, Reader(settings, source))
    with TestClient(
        restarted.http_app(stateless_http=True), base_url=settings.public_base_url
    ) as client:
        if has_refresh:
            refreshed = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": tokens["refresh_token"],
                    "scope": "read:user",
                },
            )
            assert refreshed.status_code == 200, refreshed.text
            token = refreshed.json()["access_token"]
        else:
            assert "refresh_token" not in tokens
            token = tokens["access_token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"},
                },
            },
        )
        assert response.status_code == 200, response.text
        read = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search_notes", "arguments": {"query": "zaloha"}},
            },
        )
        assert read.status_code == 200 and "notes/Backup.md" in read.text
        user["id"] = 43
        assert (
            client.post(
                "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            ).status_code
            == 401
        )
    # Durable token storage is encrypted; no plaintext upstream token in storage files.
    for file in (settings.data_root / "auth" / "store").rglob("*"):
        if file.is_file():
            assert b"synthetic-upstream-secret" not in file.read_bytes()
