"""Git credential protocol helper. Secrets enter through child environment only."""

import os
import sys
from urllib.parse import urlsplit


def main():
    if len(sys.argv) != 2 or sys.argv[1] != "get":
        return
    fields = dict(line.rstrip("\n").split("=", 1) for line in sys.stdin if "=" in line)
    url = urlsplit(os.environ["VAULT_GIT_URL"])
    if (
        fields.get("protocol") == "https"
        and fields.get("host") == url.netloc
        and fields.get("path", "").lstrip("/") == url.path.lstrip("/")
    ):
        print("username=" + os.environ["VAULT_GIT_USERNAME"])
        print("password=" + os.environ["VAULT_GIT_PAT"])
        print()


if __name__ == "__main__":
    main()
