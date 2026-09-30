"""Passive Markdown metadata extraction; raw bytes remain authoritative."""

import hashlib
import json
import re
from pathlib import PurePosixPath

import yaml

from .policy import normalize


def strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def bounded_yaml(text: str) -> dict:
    if len(text.encode()) > 16384:
        raise ValueError("Frontmatter size limit")
    # Inspect events first: reject aliases and bound nesting/node count before constructing values.
    depth = count = 0
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        count += 1
        if count > 2048 or isinstance(event, yaml.AliasEvent):
            raise ValueError("Frontmatter node/alias limit")
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
        if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1
        if depth > 16:
            raise ValueError("Frontmatter depth limit")
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict) or any(not isinstance(k, str) for k in data):
        raise ValueError("Frontmatter must be an object")
    # JSON also normalizes dates while preserving scalar bool/number types.
    return json.loads(json.dumps(data, default=str, allow_nan=False))


def parse_note(path: str, raw: bytes) -> dict:
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    frontmatter, warnings = {}, []
    body_start = 0
    if lines and lines[0].strip() == "---":
        closing = next(
            (i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None
        )
        if closing is None:
            warnings.append("INVALID_FRONTMATTER")
        else:
            body_start = closing + 1
            try:
                frontmatter = bounded_yaml("".join(lines[1:closing]))
            except (yaml.YAMLError, ValueError, RecursionError):
                warnings.append("INVALID_FRONTMATTER")
    headings, blocks, links, body_tags = [], [], [], []
    fence = None
    for i, line in enumerate(lines[body_start:], body_start + 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            kind = marker[1][0]
            if fence is None:
                fence = (kind, len(marker[1]))
            elif kind == fence[0] and len(marker[1]) >= fence[1]:
                fence = None
            continue
        if fence:
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            headings.append({"level": len(heading[1]), "title": heading[2], "line": i})
        blocks.extend({"id": m[1], "line": i} for m in re.finditer(r"(?:^|\s)\^([\w-]+)\s*$", line))
        body_tags.extend(m[1] for m in re.finditer(r"(?<![\w/#])#([\w]+(?:/[\w-]+)*)", line))
        for m in re.finditer(r"(!?)\[\[([^\]]+)\]\]", line):
            target = m[2].split("|", 1)[0]
            name, _, anchor = target.partition("#")
            links.append(
                {"target": name, "anchor": anchor, "embed": bool(m[1]), "line": i, "kind": "wiki"}
            )
        for m in re.finditer(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)", line):
            name, _, anchor = m[1].partition("#")
            links.append(
                {"target": name, "anchor": anchor, "embed": False, "line": i, "kind": "markdown"}
            )
    title = frontmatter.get("title")
    if not isinstance(title, str):
        title = next((h["title"] for h in headings if h["level"] == 1), PurePosixPath(path).stem)
    aliases = strings(frontmatter.get("aliases", frontmatter.get("alias")))
    tags = sorted({normalize(t.lstrip("#")) for t in strings(frontmatter.get("tags")) + body_tags})
    return {
        "path": path,
        "text": text,
        "title": title,
        "aliases": aliases,
        "tags": tags,
        "frontmatter": frontmatter,
        "warnings": warnings,
        "headings": headings,
        "blocks": blocks,
        "links": links,
        "revision": hashlib.sha256(raw).hexdigest(),
        "line_count": len(lines),
        "byte_count": len(raw),
    }
