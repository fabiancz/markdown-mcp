"""Measure full read calls with real HTTPS Git on synthetic 1k/10k corpora."""

import asyncio
import json
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from evaluate import ROOT, corpus
from git_fixture import https_git

from obsidian_mcp.config import Settings
from obsidian_mcp.read import Reader


def git(root, *args):
    if args[0] != "init":
        actual = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"], text=True
        ).strip()
        assert actual == str(root)
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


async def measure(size):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        origin = root / "owner/vault.git"
        origin.mkdir(parents=True)
        git(origin, "init", "-b", "main")
        git(origin, "config", "user.name", "Fixture")
        git(origin, "config", "user.email", "fixture@example.invalid")
        for path, raw in corpus(size):
            file = origin / path
            file.parent.mkdir(exist_ok=True)
            file.write_bytes(raw)
        git(origin, "add", ".")
        git(origin, "commit", "-m", "test: network evaluation corpus")
        with https_git(root) as (port, cert, _):
            settings = Settings(
                deployment_mode="tunnel",
                git_provider="forgejo",
                git_repo_url=f"https://localhost:{port}/owner/vault.git",
                git_username="fixture",
                git_pat="test-only",
                git_ca_bundle=cert,
                repo_root=root / "repo",
                data_root=root / "data",
            )
            reader = Reader(settings)
            start = time.perf_counter()
            await reader.search_notes("zaloha")
            first_seconds = time.perf_counter() - start
            durations = []
            for _ in range(20):
                start = time.perf_counter()
                await reader.search_notes("zaloha")
                durations.append((time.perf_counter() - start) * 1000)
            return {
                "notes": size,
                "initial_clone_fetch_index_read_seconds": round(first_seconds, 3),
                "unchanged_fetch_read_p50_ms": round(statistics.median(durations), 3),
                "unchanged_fetch_read_p95_ms": round(sorted(durations)[18], 3),
                "fetches": reader.source.fetch_count,
                "index_builds": reader.index.build_count,
            }


async def main():
    report = {
        "date": "2026-09-30",
        "platform": platform.platform(),
        "scope": (
            "Disposable localhost TLS/Basic-auth smart Git. Includes actual clone, "
            "fetch, object extraction, index publication and read. No WAN latency "
            "or private vault; host not constrained to 2 vCPU."
        ),
        "metrics": [await measure(size) for size in [1000, 10000]],
    }
    (ROOT / "docs/search-network-evaluation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
