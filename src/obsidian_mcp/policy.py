import unicodedata

from .models import DomainError, Principal


def normalize(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn"
    )


def path_key(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def safe_path(path: str, *, folder: bool = False) -> str:
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\0" in path
        or any(p in ("", ".", "..") for p in path.rstrip("/").split("/"))
    ):
        raise DomainError("INVALID_PATH", "Use a relative POSIX vault path")
    return path.rstrip("/") if folder else path


class Policy:
    def __init__(self, allowed: str = "", excluded: str = ".git,.obsidian,.trash,repo,data"):
        self.allowed = tuple(
            safe_path(p.strip(), folder=True) for p in allowed.split(",") if p.strip()
        )
        self.excluded = tuple(
            safe_path(p.strip(), folder=True) for p in excluded.split(",") if p.strip()
        )

    def permits(self, path: str) -> bool:
        try:
            safe_path(path)
        except DomainError:
            return False
        parts = path.split("/")
        if any(p in {".git", ".obsidian", ".trash"} for p in parts):
            return False
        if any(path == p or path.startswith(p + "/") for p in self.excluded):
            return False
        return not self.allowed or any(path.startswith(p + "/") for p in self.allowed)

    def authorize(self, principal: Principal, operation: str) -> None:
        if (
            operation not in {"read", "write"}
            or ("reader" if operation == "read" else "writer") not in principal.roles
        ):
            raise DomainError(
                "PERMISSION_DENIED",
                "The principal is not permitted to perform this vault operation",
            )
