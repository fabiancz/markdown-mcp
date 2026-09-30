"""Reproducible synthetic retrieval evaluation; never accesses a private vault."""

import asyncio
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from obsidian_mcp.config import Settings
from obsidian_mcp.read import Reader, compile_query

ROOT = Path(__file__).resolve().parents[1]
TOPICS = [
    (
        "Backup",
        "záloha",
        "backup",
        "Database restore with docker compose, 192.0.2.1 and https://example.invalid/a?b=c.",
    ),
    ("Network", "síť", "network", "Router firewall and packet routing."),
    ("Certificates", "certifikáty", "certificates", "TLS renewal and certificate trust."),
    ("Deployment", "nasazení", "deployment", "Release application images with rollback."),
    ("Monitoring", "sledování", "monitoring", "Metrics alerts and service health."),
    ("Database", "databáze", "database", "SQLite transactions and schema migrations."),
    ("Cache", "mezipaměť", "cache", "Eviction TTL and stale entries."),
    ("Security", "bezpečnost", "security", "Principals policies and access controls."),
    ("Containers", "kontejnery", "containers", "Docker image runtime volumes and permissions."),
    ("Git", "větve", "branches", "Git commit trees and immutable snapshots."),
    ("Storage", "úložiště", "storage", "Disk quotas persistent mounts and recovery."),
    ("Calendar", "kalendář", "calendar", "Meetings schedule and reminders."),
    ("Travel", "cestování", "travel", "Packing list and train tickets."),
    ("Cooking", "vaření", "cooking", "Ingredients recipe and preparation."),
    ("Reading", "čtení", "reading", "Book notes chapters and bibliography."),
    ("Research", "výzkum", "research", "Evidence hypotheses experiments and citations."),
    ("Projects", "projekty", "projects", "Planning milestones tasks and reviews."),
    ("Budget", "rozpočet", "budget", "Cost estimates and monthly expenditure."),
    ("Garden", "zahrada", "garden", "Watering seasonal planting and compost."),
    ("Writing", "psaní", "writing", "Draft editing outline and revision."),
]


def corpus(size):
    files = []
    for name, cz, en, body in TOPICS:
        text = (
            f"---\naliases: [{cz}, {en}]\ntags: [reference]\nactive: true\n---\n"
            f"# {name}\n## Guide\n{body}\n"
        )
        files.append((f"topics/{name}.md", text.encode()))
    for i in range(size - len(files)):
        name, cz, en, body = TOPICS[i % len(TOPICS)]
        text = f"# Journal entry {i}\nGeneral daily record. Mention: {cz} {en}.\n{body}\n" * 2
        files.append((f"archive/{i:05}.md", text.encode()))
    return files


async def measure(size, cases):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = Settings(
            deployment_mode="tunnel",
            git_provider="forgejo",
            git_repo_url="https://fixture.invalid/owner/vault.git",
            git_username="fixture",
            git_pat="synthetic-only",
            repo_root=root / "repo",
            data_root=root / "data",
        )
        reader = Reader(settings)
        files = corpus(size)
        start = time.perf_counter()
        snapshot = reader.index.publish("a" * 40, files, [])
        build = time.perf_counter() - start
        durations, ranks, baseline = [], [], []
        for case in cases:
            # Warm FTS pages before recording service execution and response generation.
            await reader.search_notes(snapshot_id=snapshot, **case["request"])
            start = time.perf_counter()
            response = await reader.search_notes(snapshot_id=snapshot, **case["request"])
            durations.append((time.perf_counter() - start) * 1000)
            paths = [h["path"] for h in response["data"]["items"]]
            ranks.append(paths.index(case["expected"]) + 1 if case["expected"] in paths else 0)
            if case["request"].get("mode", "fulltext") == "fulltext":
                expression, _ = compile_query(case["request"]["query"])
                with reader.index.connect() as db:
                    rows = db.execute(
                        "SELECT n.path,n.document FROM notes_fts "
                        "JOIN notes n ON n.id=notes_fts.rowid "
                        "WHERE notes_fts MATCH ? AND n.snapshot=? "
                        "ORDER BY bm25(notes_fts),n.path",
                        (expression, snapshot),
                    ).fetchall()
                baseline_paths = []
                request = case["request"]
                for row in rows:
                    document = json.loads(row["document"])
                    if request.get("folder") and not document["path"].startswith(
                        request["folder"] + "/"
                    ):
                        continue
                    if not set(request.get("tags", [])).issubset(document["tags"]):
                        continue
                    if any(
                        document["frontmatter"].get(k) != v
                        for k, v in request.get("frontmatter", {}).items()
                    ):
                        continue
                    baseline_paths.append(row[0])
                    if len(baseline_paths) == 5:
                        break
                baseline.append(case["expected"] in baseline_paths)
        ordered = sorted(durations)
        return {
            "notes": size,
            "index_build_seconds": round(build, 3),
            "warm_search_p50_ms": round(statistics.median(durations), 3),
            "warm_search_p95_ms": round(ordered[int(len(ordered) * 0.95)], 3),
            "top5_recall": round(sum(rank > 0 for rank in ranks) / len(ranks), 4),
            "mrr_at_5": round(sum(1 / r if r else 0 for r in ranks) / len(ranks), 4),
            "unweighted_bm25_fulltext_top5_recall": round(sum(baseline) / len(baseline), 4),
        }


async def main():
    cases = json.loads((ROOT / "tests/fixtures/search_eval.json").read_text())
    metrics = [await measure(size, cases) for size in [1000, 10000]]
    report = {
        "date": "2026-09-30",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "cases": len(cases),
        "metrics": metrics,
        "scope": (
            "Synthetic alias/title and passage retrieval; warm service calls pinned "
            "to an indexed generation. No network fetch. Host resources are not "
            "constrained to 2 vCPU. Not a private-vault or production benchmark."
        ),
    }
    (ROOT / "docs/search-evaluation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if any(m["top5_recall"] < 1 for m in metrics):
        raise SystemExit("Expected retrieval case failed")


if __name__ == "__main__":
    asyncio.run(main())
