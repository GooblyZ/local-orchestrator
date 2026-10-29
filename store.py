"""Persistence: atomic JSON state, redacted JSONL event log, control file, workspace locks."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from safety import Redactor, clip, pid_alive


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


REPLACE_RETRY_SEC = 5.0  # total time to retry a replace that Windows refuses because a reader has the target open


def _transient_lock(e: OSError) -> bool:
    """Windows refuses to replace a file another handle has open (e.g. the browser viewer reading state.json):
    WinError 5 (access denied), 32 (sharing violation) or 33 (lock violation). These clear by themselves."""
    return isinstance(e, PermissionError) or getattr(e, "winerror", None) in (5, 32, 33)


def atomic_write_json(path, obj) -> None:
    """Serialise first (a bad object never touches the file), then a UNIQUE temp file + fsync + replace.
    The replace is retried (bounded) while Windows reports a transient lock; readers always see the old or the new
    file, never a partial one. If it still fails, the original error is raised and the temp file is removed."""
    data = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        deadline, delay = time.monotonic() + REPLACE_RETRY_SEC, 0.005
        while True:
            try:
                os.replace(tmp, path)
                return
            except OSError as e:
                if not _transient_lock(e) or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class EventLog:
    """Append-only JSONL; every record is redacted and size-clipped before it is written."""

    def __init__(self, path, redactor: Redactor, max_str: int = 4000):
        self.path, self.redactor, self.max_str = Path(path), redactor, max_str
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                self.seq = sum(1 for _ in f)

    def _clip(self, o):
        if isinstance(o, str):
            return clip(o, self.max_str)
        if isinstance(o, dict):
            return {k: self._clip(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self._clip(v) for v in o[:200]]
        return o

    def write(self, event: str, cycle=None, **data) -> None:
        self.seq += 1
        rec = {"seq": self.seq, "ts": now(), "event": event, "cycle": cycle,
               "data": self._clip(self.redactor.obj(data))}
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- control (pause/stop)
def write_control(run_dir, command: str) -> None:
    atomic_write_json(Path(run_dir) / "control.json", {"command": command, "ts": now()})


def read_control(run_dir) -> str | None:
    p = Path(run_dir) / "control.json"
    try:
        return read_json(p).get("command")
    except (OSError, ValueError):
        return None


def clear_control(run_dir) -> None:
    try:
        (Path(run_dir) / "control.json").unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- workspace lock
class WorkspaceLocked(Exception):
    pass


class ActiveTaskExists(Exception):
    pass


def _lock_file(runs_dir, workspace) -> Path:
    key = os.path.normcase(os.path.realpath(workspace))
    return Path(runs_dir) / "_locks" / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".lock")


def acquire_lock(runs_dir, workspace, task_id: str) -> Path:
    path = _lock_file(runs_dir, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = read_json(path)
            except (OSError, ValueError):
                holder = {}
            if pid_alive(int(holder.get("pid", 0))):
                raise WorkspaceLocked(
                    f"workspace is in use by task {holder.get('task_id')} (pid {holder.get('pid')})")
            try:  # stale lock from a dead process
                path.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"task_id": task_id, "pid": os.getpid(), "ts": now()}, f)
        return path
    raise WorkspaceLocked("could not acquire workspace lock")


def release_lock(path, task_id: str) -> None:
    try:
        if read_json(path).get("task_id") == task_id:
            Path(path).unlink()
    except (OSError, ValueError):
        pass


ACTIVE = ("RUNNING", "PAUSED", "HUMAN_REQUIRED")


def find_active_tasks(runs_dir, workspace, exclude: str | None = None) -> list[str]:
    key = os.path.normcase(os.path.realpath(workspace))
    out = []
    for sf in Path(runs_dir).glob("*/state.json"):
        try:
            st = read_json(sf)
        except (OSError, ValueError):
            continue
        if (st.get("task_id") != exclude and st.get("status") in ACTIVE
                and os.path.normcase(os.path.realpath(st.get("workspace", ""))) == key):
            out.append(st["task_id"])
    return out
