"""Complete research material for Codex's independent review - never silently truncated.

Every text file of the research workspace (the report first) is split into traceable segments (file, line range, sha256)
and packed into parts that fit the reviewer's prompt budget. One part: Codex reviews the COMPLETE material in one call.
Several parts: Codex reviews parts 1..n-1 in separate calls and writes notes; the final review call receives those notes,
the last part's text and a coverage manifest that accounts for every segment. Files that cannot be included (binary,
too large) are listed in the manifest, never dropped silently. Nothing here contacts a model.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from goal_mode import RESEARCH_REPORT

SKIP_DIRS = {".git", ".orchestrator", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
MAX_FILE_BYTES = 5_000_000
PART_NOTE_FIELDS = ("verified_claims", "weak_or_unsupported_claims", "contradictions", "criteria_notes", "web_checks")
PART_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["segments_reviewed", *PART_NOTE_FIELDS],
    "properties": {"segments_reviewed": {"type": "array", "items": {"type": "string"}},
                   **{k: {"type": "array", "items": {"type": "string"}} for k in PART_NOTE_FIELDS}}}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect(root) -> tuple[list, list]:
    """(files, skipped). files: [{"path", "text"}], RESEARCH_REPORT.md first, then sorted paths."""
    root = Path(root)
    files, skipped = [], []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if name.startswith(".") or p.is_symlink():
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    skipped.append({"path": rel, "reason": f"larger than {MAX_FILE_BYTES:,} bytes"})
                    continue
                text = p.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                skipped.append({"path": rel, "reason": "not UTF-8 text (binary)"})
                continue
            except OSError as e:
                skipped.append({"path": rel, "reason": f"unreadable: {type(e).__name__}"})
                continue
            files.append({"path": rel, "text": text})
    files.sort(key=lambda f: (f["path"] != RESEARCH_REPORT, f["path"]))
    return files, skipped


def _pieces(text: str, max_chars: int) -> list:
    """Split at line boundaries, preferring Markdown headings, into pieces of at most max_chars: [(first_line, last_line, text)]."""
    lines = text.splitlines(keepends=True) or [""]
    out, cur, start = [], [], 1
    size = 0

    def flush(end):
        nonlocal cur, size, start
        if cur:
            out.append((start, end, "".join(cur)))
        cur, size, start = [], 0, end + 1

    for i, line in enumerate(lines, 1):
        while len(line) > max_chars:  # a single enormous line is cut by characters (still fully covered)
            flush(i - 1)
            out.append((i, i, line[:max_chars]))
            line = line[max_chars:]
            start = i
        heading = re.match(r"#{1,4}\s", line)
        if cur and (size + len(line) > max_chars or (heading and size > max_chars // 2)):
            flush(i - 1)
            start = i
        cur.append(line)
        size += len(line)
    flush(len(lines))
    return out


def prepare(root, max_chars: int) -> dict:
    """Segments packed into parts of at most max_chars of text each, plus the coverage manifest."""
    files, skipped = collect(root)
    segments = []
    for f in files:
        for a, b, t in _pieces(f["text"], max_chars):
            segments.append({"id": f"S{len(segments) + 1}", "path": f["path"], "lines": [a, b], "chars": len(t),
                             "sha256": _sha(t)[:16], "text": t})
    parts, cur, size = [], [], 0
    for seg in segments:
        if cur and size + seg["chars"] > max_chars:
            parts.append(cur)
            cur, size = [], 0
        cur.append(seg)
        size += seg["chars"]
    if cur:
        parts.append(cur)
    part_of = {seg["id"]: i + 1 for i, p in enumerate(parts) for seg in p}
    manifest = {"files": [{"path": f["path"], "chars": len(f["text"]), "sha256": _sha(f["text"])[:16],
                           "segments": [{"id": s["id"], "lines": s["lines"], "part": part_of[s["id"]]}
                                        for s in segments if s["path"] == f["path"]]} for f in files],
                "not_included": skipped, "parts": len(parts), "segments": len(segments),
                "total_chars": sum(len(f["text"]) for f in files), "max_chars_per_part": max_chars}
    return {"parts": parts, "manifest": manifest}


def part_view(part: list) -> list:
    return [{k: s[k] for k in ("id", "path", "lines", "sha256", "text")} for s in part]


def validate_part_notes(d: dict, expected_ids: list) -> dict:
    """Codex's notes on one part. Lists of strings only (bounded); which expected segments it confirmed reading."""
    from agents import AdapterError
    if not isinstance(d, dict):
        raise AdapterError("malformed", "part review is not a JSON object")
    notes = {}
    for k in ("segments_reviewed",) + PART_NOTE_FIELDS:
        v = d.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise AdapterError("malformed", f"part review field '{k}' must be a list of strings")
        notes[k] = [x[:2000] for x in v[:200]]
    confirmed = [i for i in expected_ids if i in set(notes["segments_reviewed"])]
    notes["coverage"] = {"expected": list(expected_ids), "confirmed": confirmed,
                         "unconfirmed": [i for i in expected_ids if i not in confirmed]}
    return notes
