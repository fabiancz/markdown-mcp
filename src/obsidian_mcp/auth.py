"""Public FastMCP auth extensions and local tunnel principal."""

import os
from pathlib import Path

from cryptography.fernet import Fernet
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from .config import Settings
from .models import DomainError, Principal
from .policy import Policy


def persistent_secret(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_bytes()
    else:
        key = Fernet.generate_key()
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
    if len(key) != 44:
        raise DomainError(
            "AUTH_STATE_INVALID", "Persistent auth key is invalid; restore the auth directory"
        )
    return key


class AllowlistedGitHubProvider(GitHubProvider):
    def __init__(self, *, allowed_user_ids: frozenset[str], **kwargs):
        self.allowed_user_ids = allowed_user_ids
        super().__init__(**kwargs)

    async def verify_token(self, token: str):
        result = await super().verify_token(token)
        if result is None or str(result.claims.get("sub", "")) not in self.allowed_user_ids:
            return None
        return result


def make_auth(settings: Settings):
    if settings.deployment_mode == "tunnel":
        return None
    directory = settings.data_root / "auth"
    key = persistent_secret(directory / "storage.key")
    signing = persistent_secret(directory / "signing.key")
    return AllowlistedGitHubProvider(
        allowed_user_ids=settings.allowed_user_ids,
        client_id=settings.github_oauth_client_id,
        client_secret=settings.github_oauth_client_secret.get_secret_value(),
        base_url=settings.public_base_url.rstrip("/"),
        required_scopes=["read:user"],
        allowed_client_redirect_uris=settings.redirect_uris,
        client_storage=FernetEncryptionWrapper(
            DiskStore(directory=directory / "store"), fernet=Fernet(key)
        ),
        jwt_signing_key=signing,
        require_authorization_consent=True,
        enable_cimd=False,
    )


def principal(settings: Settings) -> Principal:
    if settings.deployment_mode == "tunnel":
        return Principal(
            subject="tunnel_operator",
            roles=frozenset({"reader", "writer"} if settings.write_enabled else {"reader"}),
        )
    token = get_access_token()
    if (
        token is None
        or str(token.claims.get("sub", "")) not in settings.allowed_user_ids
        or "read:user" not in token.scopes
    ):
        raise DomainError(
            "PERMISSION_DENIED", "The current identity is not permitted to access this vault"
        )
    return Principal(
        subject="github:" + str(token.claims["sub"]),
        roles=frozenset(
            {"reader", "writer"}
            if settings.write_enabled and str(token.claims["sub"]) in settings.write_user_ids
            else {"reader"}
        ),
    )


class AuthorizeRequests(Middleware):
    def __init__(self, settings: Settings):
        self.settings = settings

    async def on_request(self, context, call_next):
        try:
            Policy().authorize(principal(self.settings), "read")
        except DomainError as error:
            raise ToolError(error.message) from None
        return await call_next(context)
