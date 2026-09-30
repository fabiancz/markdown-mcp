import asyncio
import hashlib
import json
import time

import pytest
from conftest import git

from obsidian_mcp.git_source import ManagedGit
from obsidian_mcp.models import DomainError
from obsidian_mcp.read import Reader


async def test_search_read_and_metadata(vault):
    settings, source, _, files = vault
    reader = Reader(settings, source)
    result = await reader.search_notes("zaloha")
    hits = result["data"]["items"]
    assert hits[0]["path"] == "notes/Backup.md"
    snapshot = result["meta"]["snapshot_id"]
    assert hits[0]["revision"] == hashlib.sha256(files["notes/Backup.md"].encode()).hexdigest()
    for hit in hits:
        note = await reader.read_note(hit["path"], snapshot)
        for snippet in hit["snippets"]:
            lines = note["data"]["text"].splitlines(keepends=True)
            assert snippet["text"] in "".join(
                lines[snippet["line_start"] - 1 : snippet["line_end"]]
            )
    assert (
        len(
            (
                await reader.search_notes(
                    "zaloha", folder="notes", tags=["ops"], frontmatter={"active": True}
                )
            )["data"]["items"]
        )
        == 1
    )
    assert not (await reader.search_notes("zaloha", frontmatter={"active": 1}))["data"]["items"]
    assert not (await reader.search_notes("Readable", folder="note"))["data"]["items"]
    for query in ["https://example.invalid/a?b=c", "192.0.2.1", "docker compose", "foo:bar-baz"]:
        assert (await reader.search_notes(query, mode="literal"))["data"]["items"][0][
            "path"
        ] == "notes/Backup.md"
    assert (await reader.search_notes("backup guide", mode="filename"))["data"]["items"][0][
        "path"
    ] == "notes/Backup.md"
    assert (await reader.search_notes('"docker compose"'))["data"]["items"]
    outline = (await reader.get_note_outline("notes/Backup.md", snapshot))["data"]
    assert outline["headings"][1] == {"level": 2, "title": "Restore", "line": 8}
    assert outline["blocks"] == [{"id": "restore", "line": 11}]
    links = (await reader.get_backlinks("notes/Backup.md", snapshot))["data"]["items"]
    assert len(links) == 3 and {link["path"] for link in links} == {"notes/Body.md"}
    broken = await reader.read_note("notes/Broken.md", snapshot)
    assert broken["data"]["parse_warnings"] == ["INVALID_FRONTMATTER"]
    assert "Readable" in broken["data"]["text"]
    body = reader.index.note(snapshot, "notes/Body.md")
    assert body["tags"] == ["projekt/backend"]
    assert not (await reader.read_note("notes/Empty.md", snapshot))["data"]["text"]


async def test_sync_concurrency_updates_restart_and_dirty_checkout(vault):
    settings, source, origin, _ = vault
    reader = Reader(settings, source)
    results = await asyncio.gather(*(reader.search_notes("zaloha") for _ in range(12)))
    assert source.fetch_count == 1 and reader.index.build_count == 1
    old = results[0]["meta"]["snapshot_id"]
    await reader.list_notes()
    assert source.fetch_count == 2 and reader.index.build_count == 1
    (source.checkout / "local.md").write_text("Uncommitted private change")
    (origin / "notes" / "Body.md").unlink()
    git(origin, "mv", "notes/Backup.md", "notes/Renamed.md")
    (origin / "notes" / "Fresh.md").write_text("# Fresh\nnewunique\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "test: change fixture")
    new = await reader.list_notes(limit=50)
    assert new["meta"]["snapshot_id"] != old
    assert "DIRTY_CHECKOUT_IGNORED" in new["meta"]["warnings"]
    paths = {hit["path"] for hit in new["data"]["items"]}
    assert "notes/Fresh.md" in paths and "notes/Renamed.md" in paths
    assert (
        "notes/Backup.md" not in paths and "notes/Body.md" not in paths and "local.md" not in paths
    )
    assert (source.checkout / "local.md").read_text() == "Uncommitted private change"
    assert (await reader.read_note("notes/Backup.md", old))["data"]["title"] == "Backups"
    before = source.fetch_count
    restarted = Reader(settings, source)
    assert (await restarted.read_note("notes/Backup.md", old))["data"]["revision"]
    assert source.fetch_count == before
    await restarted.list_notes()
    assert restarted.index.build_count == 0
    assert git(source.checkout, "rev-parse", "HEAD") != git(origin, "rev-parse", "HEAD")


async def test_offline_strict_no_snapshot_and_cancellation(vault, monkeypatch):
    settings, source, _, _ = vault
    reader = Reader(settings, source)
    old = await reader.snapshot()

    def failure():
        raise DomainError("SYNC_UNAVAILABLE", "Failed", True)

    monkeypatch.setattr(source, "sync", failure)
    stale = await reader.list_notes()
    assert stale["meta"]["stale"] and stale["meta"]["source_checked_at"] == old.source_checked_at
    assert (await reader.get_vault_status())["meta"]["stale"]
    assert not (await reader.read_note("notes/Backup.md", old.snapshot_id))["meta"]["stale"]
    settings.sync_failure_policy = "error"
    with pytest.raises(DomainError, match="No fresh snapshot"):
        await reader.list_notes()
    empty_settings = settings.model_copy(update={"data_root": settings.data_root.parent / "empty"})
    empty = Reader(empty_settings, source)
    with pytest.raises(DomainError):
        await empty.list_notes()
    original = ManagedGit.sync

    def slow():
        time.sleep(0.05)
        return original(source)

    monkeypatch.setattr(source, "sync", slow)
    cancelled = asyncio.create_task(reader.list_notes())
    await asyncio.sleep(0.01)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    other = await reader.list_notes()
    assert other["data"]["items"]


async def test_cursors_policy_expiry_and_paths(vault):
    settings, source, origin, _ = vault
    reader = Reader(settings, source)
    first = await reader.search_notes("zaloha", limit=1)
    cursor = first["data"]["next_cursor"]
    snapshot = first["meta"]["snapshot_id"]
    assert cursor
    count = source.fetch_count
    second = await reader.search_notes("zaloha", limit=1, cursor=cursor)
    assert second["data"]["items"][0]["path"] != first["data"]["items"][0]["path"]
    assert source.fetch_count == count
    for bad in [cursor[:-2] + "ZZ", "garbage"]:
        with pytest.raises(DomainError) as error:
            await reader.search_notes("zaloha", limit=1, cursor=bad)
        assert error.value.code == "INVALID_CURSOR"
    with pytest.raises(DomainError):
        await reader.search_notes("different", limit=1, cursor=cursor)
    for path in [
        "../AGENTS.md",
        "/etc/passwd",
        "notes\\Backup.md",
        "notes/../Backup.md",
        "notes/\0.md",
        ".obsidian/Hidden.md",
        "notes/Link.md",
    ]:
        with pytest.raises(DomainError):
            await reader.read_note(path, snapshot)
        with pytest.raises(DomainError):
            await reader.get_note_outline(path, snapshot)
        with pytest.raises(DomainError):
            await reader.get_backlinks(path, snapshot)
    assert not (await reader.search_notes("secret", snapshot_id=snapshot))["data"]["items"]
    (origin / "notes" / "Fresh.md").write_text("fresh")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "test: next generation")
    await reader.list_notes()
    with reader.index.connect() as db:
        db.execute("UPDATE snapshots SET created=0 WHERE id=?", (snapshot,))
    with pytest.raises(DomainError) as error:
        await reader.read_note("notes/Backup.md", snapshot)
    assert error.value.code == "SNAPSHOT_EXPIRED"
    narrowed = settings.model_copy(update={"allowed_folders": "noteworthy"})
    updated = Reader(narrowed, source)
    with pytest.raises(DomainError):
        await updated.search_notes("zaloha", limit=1, cursor=cursor)
    with pytest.raises(DomainError):
        await updated.read_note("notes/Backup.md", snapshot)
    limited = await updated.search_notes("zaloha")
    assert {hit["path"] for hit in limited["data"]["items"]} == {"noteworthy/Other.md"}


async def test_limits_errors_recovery_and_atomic_publication(vault, monkeypatch):
    settings, source, origin, _ = vault
    reader = Reader(settings, source)
    first = await reader.snapshot()
    for query in ["", '"unclosed', "foo*", "body:word", "foo OR (bar)", "x" * 501]:
        with pytest.raises(DomainError) as error:
            await reader.search_notes(query)
        assert error.value.code == "INVALID_QUERY"
    for mode in ["semantic", "hybrid", "regex", "fuzzy"]:
        with pytest.raises(DomainError) as error:
            await reader.search_notes("word", mode=mode)
        assert error.value.code == "UNSUPPORTED_SEARCH_MODE"
    with pytest.raises(DomainError):
        await reader.search_notes("word", frontmatter={"active": {"$eq": True}})
    with pytest.raises(DomainError):
        await reader.list_notes(limit=51)
    with pytest.raises(DomainError):
        await reader.read_note("notes/Backup.md", first.snapshot_id, start_line=0)
    auth_marker = settings.data_root / "auth" / "marker"
    auth_marker.write_text("preserve")
    state_marker = settings.data_root / "state.sqlite"
    state_marker.write_text("preserve")
    settings.data_root.joinpath("index.sqlite").write_bytes(b"corrupted derived data")
    restored = Reader(settings, source)
    await restored.list_notes()
    assert auth_marker.read_text() == state_marker.read_text() == "preserve"
    assert list(settings.data_root.glob("index.sqlite.corrupt-*"))
    stable = restored.index.active()
    (origin / "notes" / "More.md").write_text("new content")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "test: atomic generation")

    def fail(*args):
        raise DomainError("INDEX_FAILED", "Unable to build")

    monkeypatch.setattr(restored.index, "publish", fail)
    response = await restored.list_notes()
    assert response["meta"]["stale"] and restored.index.active() == stable
    # Bounded read returns whole source lines and valid continuation.
    reader = Reader(settings, source)
    (origin / "notes" / "Large.md").write_text('line with quotes " and č\n' * 4000)
    git(origin, "add", ".")
    git(origin, "commit", "-m", "test: large note")
    page = await reader.read_note("notes/Large.md")
    assert page["data"]["truncated"] and page["data"]["next_start_line"] > 1
    assert len(json.dumps(page, ensure_ascii=False).encode()) <= settings.max_response_bytes


async def test_retention_between_snapshot_selection_and_sql_read(vault, monkeypatch):
    settings, source, _, _ = vault
    reader = Reader(settings, source)
    first = await reader.snapshot()
    original_search = reader._search

    def prune_before_search(*args):
        settings.snapshot_max_bytes = 1024
        reader.index.publish("b" * 40, [("New.md", b"# New\n")], [])
        return original_search(*args)

    monkeypatch.setattr(reader, "_search", prune_before_search)
    with pytest.raises(DomainError) as error:
        await reader.search_notes("zaloha", snapshot_id=first.snapshot_id)
    assert error.value.code == "SNAPSHOT_EXPIRED"
    with pytest.raises(DomainError) as error:
        reader.index.note(first.snapshot_id, "notes/Backup.md")
    assert error.value.code == "SNAPSHOT_EXPIRED"
