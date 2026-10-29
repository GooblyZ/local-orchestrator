"""Controlled file creation from structured proposals (no agent tools, nothing generated is ever executed).

A no-tools agent returns {"files": [{"path", "content"}], ...report fields}. This module (not the agent) decides
whether the proposal is acceptable and, only then, writes it through the guarded file gateway:

  make_file_spec()  the OWNER's per-task allowlist (create / overwrite paths, required definitions). Agents cannot
              widen it.
  validate()  all-or-nothing: ANY problem rejects the WHOLE proposal and nothing is written. Rejected:
              absolute paths, drive letters/UNC, "..", ADS ":", reserved device names, 8.3 short names, paths not
              in the allowlist, protected files, symlinks/junctions in the path, directories, unexpected
              overwrites, duplicate paths, deletions, oversize/odd content, credential-like content.
  apply()     writes a journal (incl. folders it will create) first, then each file via GuardedFS.write_atomic
              (exclusive temp file + fsync + os.replace), reads every file back, and rolls everything back on any
              failure.
  recover()   after a crash (journal left "pending") either completes (every file present with the expected
              hash) or rolls back; never leaves a half-applied proposal behind.
  static_evidence()  deterministic verification WITHOUT executing the code: file hash, Python syntax via
              ast.parse (no import, no exec), required top-level definitions.

HONEST SCOPE: this is a write gateway, not an OS sandbox. See README "Phase 4d" for the remaining path-race risk.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from pathlib import Path

from agents import AdapterError, validate_report
from safety import GuardedFS, Policy, PolicyViolation, Redactor, clip
from store import atomic_write_json, now

_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
DEFAULT_LIMITS = {"max_files": 20, "max_file_bytes": 200_000}


class ProposalRejected(PolicyViolation):
    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


class ApplyFailed(Exception):
    pass


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def normalize_rel(path) -> str:
    """Return a clean relative posix path or raise ValueError(reason)."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    if len(path) > 240 or re.search(r"[\x00-\x1f\x7f]", path):
        raise ValueError("path is too long or contains control characters")
    p = path.replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or p.startswith("//"):
        raise ValueError("absolute paths are not allowed")
    parts = p.split("/")
    if len(parts) > 8:
        raise ValueError("path is nested too deeply")
    for part in parts:
        if part == "..":
            raise ValueError("path traversal ('..') is not allowed")
        if part in ("", "."):
            raise ValueError("empty or '.' path component")
        if ":" in part:
            raise ValueError("':' in a path component (drive / alternate data stream) is not allowed")
        if part.endswith((".", " ")) or part.startswith(" "):
            raise ValueError("path component starts/ends with a space or ends with '.'")
        if part.split(".")[0].upper() in _RESERVED:
            raise ValueError("reserved device name")
        if re.search(r"~\d", part):
            raise ValueError("8.3 short names ('~1') are not allowed (they can alias other files)")
        if re.search(r'[<>"|?*]', part):
            raise ValueError("characters <>\"|?* are not allowed")
    return "/".join(parts)


def is_pattern(entry: str) -> bool:
    return "*" in entry or "?" in entry


def normalize_entry(entry) -> str:
    """An allowlist entry: an exact relative path, or a pattern where * / ? stay inside one folder and ** spans folders."""
    if not isinstance(entry, str) or not is_pattern(entry):
        return normalize_rel(entry)
    probe = normalize_rel(entry.replace("**", "x").replace("*", "x").replace("?", "x"))  # every other rule still applies
    parts = entry.replace("\\", "/").split("/")
    if len(parts) != len(probe.split("/")) or any("**" in p and p != "**" for p in parts):
        raise ValueError("'**' must be a whole path component")
    return "/".join(parts)


def pattern_regex(pattern: str):
    out, parts = [], pattern.split("/")
    for i, part in enumerate(parts):
        if part == "**":  # any number of folders; as the last component it also matches the file name
            out.append("(?:[^/]+/)*[^/]+" if i == len(parts) - 1 else "(?:[^/]+/)*")
            continue
        seg = "".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in part)
        out.append(seg + ("/" if i < len(pattern.split("/")) - 1 else ""))
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def make_file_spec(create=(), overwrite=(), required_defs=(), policy: Policy | None = None, limits=None) -> dict:
    """The owner's allowlist for one task (exact paths or patterns such as 'tests/*.py'). Raises ValueError."""
    spec = {"create": [], "overwrite": [], "required_defs": [], "limits": dict(DEFAULT_LIMITS, **(limits or {})), "applied": {}}
    for key, paths in (("create", create), ("overwrite", overwrite)):
        for p in paths:
            rel = normalize_entry(p)
            if policy is not None and is_pattern(rel):  # a pattern that could cover a protected name is refused up front
                last = pattern_regex(rel.split("/")[-1])
                samples = [pp.replace("*", "x").replace("?", "x") for pp in policy.protected] + [pp.replace("*", "") for pp in policy.protected]
                if any(last.match(x) for x in samples):
                    raise ValueError(f"{rel}: a pattern that can match protected files can never be allowlisted")
            if policy is not None and policy.is_protected(rel):
                raise ValueError(f"{rel}: a protected path can never be allowlisted")
            spec[key].append(rel)
    if not spec["create"] and not spec["overwrite"]:
        raise ValueError("the allowlist is empty")
    for name in required_defs:
        if not (isinstance(name, str) and name.isidentifier()):
            raise ValueError(f"required definition is not an identifier: {name!r}")
        spec["required_defs"].append(name)
    return spec


def validate_proposal_report(d: dict, max_turns: int):
    """Split an executor reply into its report fields and the proposed files. Wrong STRUCTURE is malformed output;
    a bad path or content is NOT malformed - it is rejected later by validate() with a reason the reviewer sees."""
    files, deletions = d.get("files", []), d.get("deletions", [])
    if not isinstance(files, list) or not all(isinstance(f, dict) and set(f) <= {"path", "content"} for f in files):
        raise AdapterError("malformed", "files must be a list of {path, content} objects")
    if not isinstance(deletions, list) or not all(isinstance(x, str) for x in deletions):
        raise AdapterError("malformed", "deletions must be a list of strings")
    report, ignored, perm = validate_report({k: v for k, v in d.items() if k not in ("files", "deletions")}, max_turns)
    return report, files, deletions, ignored, perm


class NoToolsFS:
    """Handed to the executor in proposal mode: it gets NO file or command tools. It can only propose files."""

    def __init__(self, root):
        self.root = root

    def _deny(self, *_a, **_k):
        raise PolicyViolation("proposal mode: the executor has no file or command tools; it may only propose files")

    write = write_atomic = read = delete = run = _deny


def _is_link(p) -> bool:
    return os.path.islink(p) or (hasattr(os.path, "isjunction") and os.path.isjunction(p))


class ProposalApplier:
    def __init__(self, policy: Policy, fs: GuardedFS, run_dir, spec: dict, redactor: Redactor | None = None):
        self.policy, self.fs, self.run_dir = policy, fs, Path(run_dir)
        self.spec, self.redactor = spec, redactor or Redactor()
        self.limits = dict(DEFAULT_LIMITS, **spec.get("limits", {}))
        self.limits["max_file_bytes"] = min(self.limits["max_file_bytes"], policy.max_write)
        self.root = policy.root
        self.jdir = self.run_dir / "apply"
        self._create = {p.lower(): p for p in spec.get("create", []) if not is_pattern(p)}
        self._overwrite = {p.lower(): p for p in spec.get("overwrite", []) if not is_pattern(p)}
        self._create_pat = [pattern_regex(p) for p in spec.get("create", []) if is_pattern(p)]
        self._overwrite_pat = [pattern_regex(p) for p in spec.get("overwrite", []) if is_pattern(p)]

    def _allowed(self, rel: str):
        """-> (canonical path, may_overwrite) or (None, False). Exact entries win; patterns match the proposed path."""
        key = rel.lower()
        if key in self._create or key in self._overwrite:
            return self._create.get(key) or self._overwrite.get(key), key in self._overwrite
        if any(r.match(rel) for r in self._overwrite_pat):
            return rel, True
        if any(r.match(rel) for r in self._create_pat):
            return rel, False
        return None, False

    # ------------------------------------------------------------------ validation (no writes)
    def validate(self, files, deletions=()) -> list[dict]:
        reasons: list[str] = []
        if not isinstance(files, list):
            raise ProposalRejected(["files must be a list"])
        if deletions:
            reasons.append(f"deletions are not permitted: {[str(x)[:80] for x in list(deletions)[:5]]}")
        if len(files) > self.limits["max_files"]:
            reasons.append(f"too many files ({len(files)} > {self.limits['max_files']})")
        planned, seen = [], set()
        for f in files[: self.limits["max_files"]]:
            raw = f.get("path") if isinstance(f, dict) else None
            try:
                rel = normalize_rel(raw)
            except ValueError as e:
                reasons.append(f"path {str(raw)[:80]!r}: {e}")
                continue
            key = rel.lower()
            if key in seen:
                reasons.append(f"{rel}: duplicate path in one proposal")
                continue
            seen.add(key)
            canonical, may_overwrite = self._allowed(rel)
            if self.policy.is_protected(rel) or (canonical and self.policy.is_protected(canonical)):
                reasons.append(f"{rel}: protected file")
                continue
            if not canonical:
                reasons.append(f"{rel}: not in the owner's allowlist for this task")
                continue
            target = self.root / canonical
            problem = self._path_problem(canonical, target)
            if problem:
                reasons.append(f"{canonical}: {problem}")
                continue
            content = f.get("content")
            problem = self._content_problem(content)
            if problem:
                reasons.append(f"{canonical}: {problem}")
                continue
            data = content.encode("utf-8")
            exists = os.path.lexists(target)
            prior = sha256_bytes(target.read_bytes()) if exists else None
            if exists:
                ours = self.spec.get("applied", {}).get(canonical) == prior  # this task wrote exactly that content
                if not may_overwrite and not ours:
                    reasons.append(f"{canonical}: unexpected overwrite (file exists and was not authorized for overwrite)")
                    continue
            planned.append({"rel": canonical, "data": data, "sha256": sha256_bytes(data),
                            "action": "overwrite" if exists else "create", "prior_sha256": prior})
        if not planned and not reasons:
            reasons.append("the proposal contains no files")
        if reasons:
            raise ProposalRejected(reasons)
        return planned

    def _path_problem(self, rel: str, target: Path) -> str | None:
        try:
            self.policy.check_path(rel, "write")
        except PolicyViolation as e:
            return str(e)
        cur = self.root
        for part in rel.split("/"):
            cur = cur / part
            if os.path.lexists(cur) and _is_link(str(cur)):
                return "symlink/junction in the path"
        if os.path.isdir(target):
            return "target is a directory"
        return None

    def _content_problem(self, content) -> str | None:
        if not isinstance(content, str):
            return "content must be a string"
        if "\x00" in content:
            return "content contains NUL"
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            return "content is not valid UTF-8"
        if len(data) > self.limits["max_file_bytes"]:
            return f"content too large ({len(data)} bytes)"
        if self.redactor.text(content) != content:
            return "content contains credential-like text (refusing to write it)"
        return None

    # ------------------------------------------------------------------ apply (journal -> atomic writes -> read-back)
    def _missing_dirs(self, rel: str) -> list[str]:
        out, cur = [], self.root
        for part in rel.split("/")[:-1]:
            cur = cur / part
            if not cur.exists():
                out.append(os.path.relpath(cur, self.root).replace("\\", "/"))
        return out

    def apply(self, planned: list[dict], cycle: int) -> list[dict]:
        self.jdir.mkdir(parents=True, exist_ok=True)
        jpath = self.jdir / f"cycle{cycle:03d}.json"
        bdir = self.jdir / f"cycle{cycle:03d}_backups"
        entries = []
        for i, p in enumerate(planned):
            backup = None
            if p["action"] == "overwrite":
                bdir.mkdir(exist_ok=True)
                backup = str(bdir / f"{i}.bak")
                Path(backup).write_bytes((self.root / p["rel"]).read_bytes())
            entries.append({"path": p["rel"], "action": p["action"], "sha256": p["sha256"], "prior_sha256": p["prior_sha256"],
                            "bytes": len(p["data"]), "backup": backup, "status": "pending"})
        new_dirs = sorted({d for p in planned for d in self._missing_dirs(p["rel"])})
        journal = {"cycle": cycle, "started": now(), "state": "pending", "entries": entries, "created_dirs": new_dirs}
        atomic_write_json(jpath, journal)  # written BEFORE any file: a crash from here on is recoverable
        try:
            for e, p in zip(entries, planned):
                self.fs.write_atomic(p["rel"], p["data"])
                if sha256_bytes((self.root / p["rel"]).read_bytes()) != p["sha256"]:
                    raise ApplyFailed(f"read-back mismatch for {p['rel']}")
                e["status"] = "applied"
                atomic_write_json(jpath, journal)
        except Exception as ex:  # BaseException (a crash) deliberately passes: the journal stays 'pending' for recover()
            self._rollback(journal, jpath)
            raise ApplyFailed(f"{type(ex).__name__}: {ex}; all changes rolled back") from ex
        journal["state"] = "complete"
        journal["finished"] = now()
        atomic_write_json(jpath, journal)
        self.spec.setdefault("applied", {}).update({e["path"]: e["sha256"] for e in entries})
        return [{k: e[k] for k in ("path", "action", "sha256", "bytes")}
                | {"lines": p["data"].count(b"\n") + (0 if p["data"].endswith(b"\n") or not p["data"] else 1), "read_back": "match"}
                for e, p in zip(entries, planned)]

    def _clean_temps(self, rel: str) -> None:
        target = self.root / rel
        if target.parent.is_dir() and not _is_link(str(target.parent)):
            for stray in target.parent.glob(f".{target.name}.orchtmp*"):
                try:
                    stray.unlink()
                except OSError:
                    pass

    def _rollback(self, journal: dict, jpath: Path) -> None:
        for e in reversed(journal["entries"]):
            target = self.root / e["path"]
            try:
                if e["action"] == "create":
                    if target.is_file() and not _is_link(str(target)) and sha256_bytes(target.read_bytes()) == e["sha256"]:
                        target.unlink()  # only ever removes the file this proposal itself created
                elif e["backup"] and os.path.exists(e["backup"]):
                    self.fs.write_atomic(e["path"], Path(e["backup"]).read_bytes())
            except (OSError, PolicyViolation):
                pass
            self._clean_temps(e["path"])
        for d in sorted(journal.get("created_dirs", []), key=lambda x: -x.count("/")):
            try:
                (self.root / d).rmdir()  # only if empty: never removes anything else
            except OSError:
                pass
        journal["state"] = "rolled_back"
        journal["finished"] = now()
        atomic_write_json(jpath, journal)

    def recover(self) -> list[str]:
        """Resolve journals left 'pending' by a crash. Returns human-readable notes."""
        notes = []
        for jpath in sorted(self.jdir.glob("cycle*.json")) if self.jdir.exists() else []:
            try:
                journal = json.loads(jpath.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if journal.get("state") != "pending":
                continue
            done = all((self.root / e["path"]).is_file() and sha256_bytes((self.root / e["path"]).read_bytes()) == e["sha256"]
                       for e in journal["entries"])
            if done:
                for e in journal["entries"]:
                    e["status"] = "applied"
                    self._clean_temps(e["path"])
                journal["state"] = "complete"
                journal["recovered"] = now()
                atomic_write_json(jpath, journal)
                self.spec.setdefault("applied", {}).update({e["path"]: e["sha256"] for e in journal["entries"]})
                notes.append(f"cycle {journal['cycle']}: an interrupted write was found complete on disk; journal closed")
            else:
                self._rollback(journal, jpath)
                notes.append(f"cycle {journal['cycle']}: an interrupted write was incomplete; ALL its changes were rolled back")
        return notes


def current_files(root, spec: dict, redactor: Redactor | None = None, max_files: int = 10, max_chars: int = 20000) -> list[dict]:
    """The CURRENT on-disk content of files the executor may change, so a correction starts from what is really there.
    Read-only, redacted, clipped; links and protected files are never read."""
    root, red = Path(root), redactor or Redactor()
    exact = [p for p in spec.get("create", []) + spec.get("overwrite", []) if not is_pattern(p)]
    pats = [pattern_regex(p) for p in spec.get("create", []) + spec.get("overwrite", []) if is_pattern(p)]
    found = [p for p in exact if (root / p).is_file()]
    if pats:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
            for n in filenames:
                rel = os.path.relpath(os.path.join(dirpath, n), root).replace("\\", "/")
                if any(r.match(rel) for r in pats) and rel not in found:
                    found.append(rel)
    out = []
    for rel in sorted(found)[:max_files]:
        p = root / rel
        if _is_link(str(p)) or any(_is_link(str(root / "/".join(rel.split("/")[:i + 1]))) for i in range(rel.count("/"))):
            continue
        try:
            text = p.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        out.append({"path": rel, "sha256": sha256_bytes(p.read_bytes()), "content": clip(red.text(text), max_chars)})
    return out


def apply_step(applier: ProposalApplier, files, deletions, cycle: int) -> dict:
    """validate -> apply. Returns the record shown to the reviewer and the viewer: status applied | rejected | failed."""
    try:
        planned = applier.validate(files, deletions)
    except ProposalRejected as e:
        return {"status": "rejected", "reasons": e.reasons, "written": [], "proposed_paths": [str(f.get("path"))[:120] for f in files if isinstance(f, dict)]}
    try:
        written = applier.apply(planned, cycle)
    except ApplyFailed as e:
        return {"status": "failed", "reasons": [str(e)], "written": [], "proposed_paths": [p["rel"] for p in planned]}
    return {"status": "applied", "reasons": [], "written": written, "proposed_paths": [p["rel"] for p in planned]}


# ---------------------------------------------------------------------- deterministic static verification
def static_evidence(root, applied: dict, required_defs) -> dict:
    """Verify what is ON DISK without importing or executing it."""
    root = Path(root)
    files, found = [], set()
    for rel in sorted(applied):
        p = root / rel
        info = {"path": rel, "exists": p.is_file(), "expected_sha256": applied[rel]}
        if info["exists"]:
            data = p.read_bytes()
            text = data.decode("utf-8", "replace")
            info.update(sha256=sha256_bytes(data), bytes=len(data), lines=text.count("\n") + (0 if text.endswith("\n") or not text else 1),
                        matches_applied=sha256_bytes(data) == applied[rel], content=clip(text, 20000))
            if rel.endswith(".py"):
                try:
                    tree = ast.parse(text, filename=rel)  # parses only; never imports or executes
                    info["python_syntax"] = "ok"
                    info["defs"] = [{"name": n.name, "args": ast.unparse(n.args), "returns": ast.unparse(n.returns) if n.returns else None}
                                    for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    found |= {d["name"] for d in info["defs"]}
                except SyntaxError as e:
                    info["python_syntax"] = f"SyntaxError: {e.msg} (line {e.lineno})"
                except Exception as e:  # RecursionError, MemoryError, ValueError ...
                    info["python_syntax"] = f"unparsable: {type(e).__name__}"
            else:
                info["python_syntax"] = "not_checked"
        files.append(info)
    req = {name: name in found for name in required_defs}
    problems = []
    if not files:
        problems.append("no files have been applied")
    for f in files:
        if not f["exists"]:
            problems.append(f"{f['path']}: missing")
        elif not f["matches_applied"]:
            problems.append(f"{f['path']}: content on disk differs from what was applied")
        elif f["python_syntax"] not in ("ok", "not_checked"):
            problems.append(f"{f['path']}: {f['python_syntax']}")
    problems += [f"required top-level definition not found: {n}" for n, ok in req.items() if not ok]
    passed = not problems
    return {"mode": "static", "executed": False, "status": "static_passed" if passed else "static_failed", "passed": passed,
            "exit_code": None, "tests_ran": None, "command": None, "files": files, "required_defs": req,
            "note": "Generated code was NOT executed. Checks: content hash read back from disk, Python syntax via ast.parse "
                    "(no import, no exec), required top-level definitions.",
            "output_tail": "; ".join(problems)}


def not_run_evidence(why: str) -> dict:
    return {"mode": "static", "executed": False, "status": "not_run", "passed": False, "exit_code": None, "tests_ran": None,
            "command": None, "files": [], "required_defs": {}, "note": why, "output_tail": why}
