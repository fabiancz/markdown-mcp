"""Environment configuration: no implicit unauthenticated deployment."""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="forbid")
    deployment_mode: Literal["oauth", "tunnel"]
    public_base_url: str = ""
    git_provider: Literal["github", "gitlab", "forgejo"]
    git_repo_url: str
    git_target_branch: str = ""
    git_username: str
    git_pat: SecretStr = SecretStr("")
    git_pat_file: Path | None = None
    forge_api_url: str = ""
    forge_repo_id: str = ""
    git_commit_name: str = "Vault MCP Bot"
    git_commit_email: str = "vault-mcp@example.invalid"
    repo_root: Path = Path("/repo")
    data_root: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 8000
    sync_before_read: bool = True
    sync_min_interval_seconds: float = 0
    sync_failure_policy: Literal["serve_stale", "error"] = "serve_stale"
    git_timeout_seconds: float = 15
    git_ca_bundle: Path | None = None
    github_oauth_client_id: str = ""
    github_oauth_client_secret: SecretStr = SecretStr("")
    github_oauth_client_secret_file: Path | None = None
    github_allowed_user_ids: str = ""
    oauth_redirect_uris: str = ""
    write_enabled: bool = False
    write_default_mode: Literal["review", "yolo"] = "review"
    yolo_enabled: bool = False
    allowed_folders: str = ""
    excluded_folders: str = ".git,.obsidian,.trash,repo,data"
    max_file_bytes: int = 1048576
    max_response_bytes: int = 32768
    max_index_bytes: int = 268435456
    snapshot_retention_seconds: int = 900
    snapshot_max_bytes: int = 1073741824

    @model_validator(mode="after")
    def validate_configuration(self):
        for name in ("git_pat", "github_oauth_client_secret"):
            file = getattr(self, name + "_file")
            if file:
                if getattr(self, name).get_secret_value():
                    raise ValueError(f"Set only {name.upper()} or {name.upper()}_FILE")
                setattr(self, name, SecretStr(file.read_text().strip()))
        url = urlsplit(self.git_repo_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not url.path.strip("/")
        ):
            raise ValueError("GIT_REPO_URL must be a credential-free HTTPS repository URL")
        if any(c in self.git_repo_url + self.git_username for c in "\n\r\0"):
            raise ValueError("Invalid Git configuration")
        if not self.git_username or not self.git_pat.get_secret_value():
            raise ValueError("Git username and PAT are required")
        if self.git_target_branch and (
            not re.fullmatch(r"[\w./-]+", self.git_target_branch)
            or self.git_target_branch.startswith(("-", "/"))
            or ".." in self.git_target_branch
            or "@{" in self.git_target_branch
        ):
            raise ValueError("Invalid target branch")
        if self.write_enabled or self.yolo_enabled or self.write_default_mode != "review":
            raise ValueError("This release supports read only; write and YOLO must remain disabled")
        if self.deployment_mode == "oauth":
            public = urlsplit(self.public_base_url)
            if (
                public.scheme != "https"
                or not public.hostname
                or public.username
                or public.query
                or public.fragment
                or public.path not in ("", "/")
            ):
                raise ValueError("OAuth requires an HTTPS PUBLIC_BASE_URL without a subpath")
            if not (
                self.github_oauth_client_id
                and self.github_oauth_client_secret.get_secret_value()
                and self.allowed_user_ids
                and self.redirect_uris
            ):
                raise ValueError(
                    "OAuth requires credentials, user IDs and exact client redirect URIs"
                )
            if any(not id.isdecimal() for id in self.allowed_user_ids):
                raise ValueError("GitHub allowlist must contain numeric user IDs")
            for uri in self.redirect_uris:
                if urlsplit(uri).scheme != "https" or "*" in uri:
                    raise ValueError("OAuth client redirect URIs must be exact HTTPS URLs")
        for name in (
            "git_timeout_seconds",
            "max_file_bytes",
            "max_response_bytes",
            "max_index_bytes",
            "snapshot_retention_seconds",
            "snapshot_max_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.sync_min_interval_seconds < 0 or not 1 <= self.port <= 65535:
            raise ValueError("Invalid interval or port")
        if self.forge_api_url:
            api = urlsplit(self.forge_api_url)
            if (
                api.scheme != "https"
                or not api.hostname
                or api.username
                or api.query
                or api.fragment
            ):
                raise ValueError("FORGE_API_URL must be a credential-free HTTPS URL")
        return self

    @property
    def allowed_user_ids(self) -> frozenset[str]:
        return frozenset(x.strip() for x in self.github_allowed_user_ids.split(",") if x.strip())

    @property
    def redirect_uris(self) -> list[str]:
        return [x.strip() for x in self.oauth_redirect_uris.split(",") if x.strip()]

    @property
    def repository_id(self) -> str:
        if self.forge_repo_id:
            return self.forge_repo_id
        path = unquote(urlsplit(self.git_repo_url).path).strip("/").removesuffix(".git")
        if self.git_provider != "gitlab" and len(path.split("/")) != 2:
            raise ValueError("Subpath hosting requires explicit FORGE_REPO_ID")
        return path

    @property
    def api_url(self) -> str:
        if self.forge_api_url:
            return self.forge_api_url.rstrip("/")
        url = urlsplit(self.git_repo_url)
        if self.git_provider == "github":
            return (
                "https://api.github.com"
                if url.hostname == "github.com"
                else f"https://{url.netloc}/api/v3"
            )
        return f"https://{url.netloc}" + ("/api/v4" if self.git_provider == "gitlab" else "/api/v1")

    @property
    def source_identity(self) -> str:
        return hashlib.sha256(self.git_repo_url.encode()).hexdigest()

    @property
    def policy_hash(self) -> str:
        values = [
            self.allowed_folders,
            self.excluded_folders,
            self.max_file_bytes,
            "parser-1",
            "fts-1",
        ]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()
