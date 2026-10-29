"""Safety primitives (standard library only).

Redaction, billing-sensitive environment scrubbing, workspace path policy, command
allowlist policy, a guarded file gateway for MOCK agents, a process runner with
timeout / cancellation / process-tree kill, and workspace snapshots used as
deterministic verification evidence.

HONEST SCOPE: GuardedFS and Policy.check_command only constrain code that goes
through them (the mock agents, and the orchestrator's own verification command).
They do NOT sandbox a real CLI subprocess. See README "Security status".
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


class PolicyViolation(Exception):
    """An operation was blocked by the workspace / command policy."""


class ApprovalRequired(Exception):
    """An operation is allowed only after explicit human approval."""

    def __init__(self, op: str, path: str):
        super().__init__(f"approval required: {op} {path}")
        self.op, self.path = op, path


class ProcessTimeout(Exception):
    pass


class ProcessCancelled(Exception):
    pass


# --------------------------------------------------------------------------- env scrubbing
_BILLING_EXACT = {
    "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT", "AZURE_OPENAI_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "CLAUDECODE",
}
_SENSITIVE_NAME = re.compile(
    r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY)", re.I)


def is_sensitive_env_name(name: str) -> bool:
    u = name.upper()
    return (u in _BILLING_EXACT or bool(_SENSITIVE_NAME.search(u))
            or u.startswith("CLAUDE_CODE_"))


def scrub_env(environ=None) -> tuple[dict, list[str]]:
    """Return (clean copy, sorted removed NAMES). Never mutates the real environment
    and never returns or logs values."""
    src = dict(os.environ if environ is None else environ)
    removed = sorted(k for k in src if is_sensitive_env_name(k))
    clean = {k: v for k, v in src.items() if k not in removed}
    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    # child processes (Claude Code's shell, the test run) find the SAME Python that runs the orchestrator, even when the
    # launching shell has no `python` on PATH (e.g. only the `py` launcher). Process-local; nothing global is changed.
    key = next((k for k in clean if k.upper() == "PATH"), "PATH")
    here = os.path.dirname(sys.executable)
    parts = clean.get(key, "").split(os.pathsep) if clean.get(key) else []
    if here and os.path.normcase(here) not in {os.path.normcase(x) for x in parts}:
        clean[key] = os.pathsep.join([here] + parts)
    return clean, removed


# --------------------------------------------------------------------------- redaction
_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/\-]{16,}=*"), "Bearer [REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
                r"\s*[=:]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r'(?i)("(?:api_?key|token|secret|password|authorization)"\s*:\s*)"[^"]*"'),
     r'\1"[REDACTED]"'),
]


class Redactor:
    """Pattern-based redaction plus exact-value redaction of sensitive env values
    (held only in memory)."""

    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self._values = sorted({v for k, v in env.items()
                               if is_sensitive_env_name(k) and len(v) >= 8},
                              key=len, reverse=True)

    def text(self, s: str) -> str:
        for v in self._values:
            s = s.replace(v, "[REDACTED]")
        for pat, rep in _PATTERNS:
            s = pat.sub(rep, s)
        return s

    def obj(self, o):
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {str(k): ("[REDACTED]" if isinstance(v, str) and _SENSITIVE_NAME.search(str(k))
                             else self.obj(v)) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(x) for x in o]
        return o


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"...[truncated {len(s) - n} chars]"


# --------------------------------------------------------------------------- policy
def _norm_exe(argv0: str) -> str:
    stem = Path(argv0).stem.lower()
    return "python" if stem in ("python", "python3", "py") or stem.startswith("python3.") else stem


class Policy:
    def __init__(self, workspace, cfg: dict):
        self.root = Path(os.path.realpath(workspace))
        self.protected = [p.lower() for p in cfg["protected_path_patterns"]]
        self.allow = cfg["command_allowlist"]
        self.deletes_require_approval = cfg["deletes_require_approval"]
        self.max_write = cfg["max_file_write_bytes"]
        self.approved: set[tuple[str, str]] = set()

    def relpath(self, path) -> str:
        if "\x00" in str(path):
            raise PolicyViolation("NUL in path")
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        real = os.path.realpath(p)  # resolves .. and symlinks (existing parts)
        try:
            inside = os.path.commonpath([os.path.normcase(str(self.root)),
                                         os.path.normcase(real)]) == os.path.normcase(str(self.root))
        except ValueError:  # different drives
            inside = False
        if not inside:
            raise PolicyViolation(f"path outside workspace: {path}")
        rel = os.path.relpath(real, self.root).replace("\\", "/")
        if rel == "." or ":" in rel:
            raise PolicyViolation(f"invalid target path: {path}")
        return rel

    def is_protected(self, rel: str) -> bool:
        rl = rel.lower()
        parts = rl.split("/")
        return any(fnmatch.fnmatch(rl, pat) or any(fnmatch.fnmatch(x, pat) for x in parts)
                   for pat in self.protected)

    def check_path(self, path, op: str) -> tuple[Path, str]:
        rel = self.relpath(path)
        if self.is_protected(rel):
            raise PolicyViolation(f"{op} on protected path: {rel}")
        return self.root / rel, rel

    def check_command(self, argv) -> None:
        if (not isinstance(argv, (list, tuple)) or not argv
                or not all(isinstance(a, str) and "\x00" not in a for a in argv)):
            raise PolicyViolation("command must be a non-empty list of strings (no shell strings)")
        exe = argv[0]
        if os.sep in exe or "/" in exe:  # explicit path: only the current interpreter is accepted
            if os.path.normcase(os.path.realpath(exe)) != os.path.normcase(os.path.realpath(sys.executable)):
                raise PolicyViolation(f"executable path not allowed: {exe}")
        head = [_norm_exe(exe)] + list(argv[1:])
        for entry in self.allow:
            n = len(entry)
            if head[:n] == [_norm_exe(entry[0])] + list(entry[1:]):
                return
        raise PolicyViolation(f"command not in allowlist: {argv[0]}")


# --------------------------------------------------------------------------- process runner
@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    duration: float


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # NEVER use os.kill(pid, 0) on Windows: it terminates the process.
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_tree(p: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass
    try:
        p.communicate(timeout=5)
    except Exception:
        pass


def run_process(argv, *, cwd, env, timeout: float | None, stdin_text: str | None = None,
                cancel_check=None, max_output: int = 200_000, on_stdout_line=None) -> ProcResult:
    """Run argv (never a shell string) with timeout and cooperative cancellation.
    timeout=None means NO wall-clock limit: the call ends when the process exits or cancel_check() returns True
    (owner stop, budget). cancel_check is polled about every 0.2 s while the process runs.
    On timeout/cancel the whole process tree is killed. If `on_stdout_line` is given, stdout lines are
    delivered to it while the process is still running (streaming); otherwise output is returned at the end."""
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    start = time.monotonic()
    p = subprocess.Popen(list(argv), cwd=str(cwd), env=env, text=True, encoding="utf-8",
                         errors="replace", stdin=subprocess.PIPE if stdin_text is not None
                         else subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, **kw)
    try:
        if on_stdout_line is not None:
            return _run_streaming(p, start, stdin_text, timeout, cancel_check, max_output, on_stdout_line)
        return _run_plain(p, start, stdin_text, timeout, cancel_check, max_output)
    except BaseException:
        # a monitor/display error (or Ctrl+C) must never leave an unmanaged agent process running
        if p.poll() is None:
            kill_tree(p)
        raise


def _run_plain(p, start, stdin_text, timeout, cancel_check, max_output) -> ProcResult:
    first = True
    while True:
        if cancel_check and cancel_check():
            kill_tree(p)
            raise ProcessCancelled("cancelled")
        if timeout is not None and time.monotonic() - start > timeout:
            kill_tree(p)
            raise ProcessTimeout(f"timeout after {timeout}s")
        try:
            out, err = p.communicate(input=stdin_text if first else None, timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            first = False
    return ProcResult(p.returncode, clip(out or "", max_output), clip(err or "", max_output),
                      time.monotonic() - start)


def _run_streaming(p, start, stdin_text, timeout, cancel_check, max_output, on_line) -> ProcResult:
    import queue
    import threading
    q: "queue.Queue[str | None]" = queue.Queue()
    out_parts: list[str] = []
    err_parts: list[str] = []

    def pump_out():
        for line in iter(p.stdout.readline, ""):
            out_parts.append(line)
            q.put(line)
        q.put(None)

    def pump_err():
        for line in iter(p.stderr.readline, ""):
            err_parts.append(line)

    def feed():
        try:
            if stdin_text is not None:
                p.stdin.write(stdin_text)
                p.stdin.close()
        except (OSError, ValueError):
            pass

    threads = [threading.Thread(target=f, daemon=True) for f in (pump_out, pump_err, feed)]
    for t in threads:
        t.start()
    done = False
    while not done:
        if cancel_check and cancel_check():
            kill_tree(p)
            raise ProcessCancelled("cancelled")
        if timeout is not None and time.monotonic() - start > timeout:
            kill_tree(p)
            raise ProcessTimeout(f"timeout after {timeout}s")
        try:
            line = q.get(timeout=0.1)
        except queue.Empty:
            continue
        if line is None:
            done = True
        else:
            try:
                on_line(line.rstrip(chr(13) + chr(10)))
            except Exception:  # a broken display callback must not break the process handling
                pass
    while True:  # stdout is closed; wait for the exit itself, still honouring cancellation (and the timeout, if any)
        try:
            p.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancel_check and cancel_check():
                kill_tree(p)
                raise ProcessCancelled("cancelled") from None
            if timeout is not None and time.monotonic() - start > timeout:
                kill_tree(p)
                raise ProcessTimeout(f"timeout after {timeout}s") from None
    for t in threads[1:2]:
        t.join(timeout=2)
    for pipe in (p.stdout, p.stderr, p.stdin):
        try:
            pipe.close()
        except Exception:
            pass
    return ProcResult(p.returncode, clip("".join(out_parts), max_output), clip("".join(err_parts), max_output),
                      time.monotonic() - start)


# --------------------------------------------------------------------------- guarded gateway
class GuardedFS:
    """File/command gateway handed to MOCK executors. Real CLI executors do not use it."""

    def __init__(self, policy: Policy, env: dict, timeout: float):
        self.policy, self.env, self.timeout = policy, env, timeout

    def write(self, path, content: str) -> None:
        p, _ = self.policy.check_path(path, "write")
        if len(content.encode("utf-8")) > self.policy.max_write:
            raise PolicyViolation("write exceeds max_file_write_bytes")
        if p.is_dir():
            raise PolicyViolation("target is a directory")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

    def _no_link_on_path(self, rel: str) -> None:
        cur = self.policy.root
        for part in rel.split("/"):
            cur = cur / part
            if os.path.islink(cur) or (hasattr(os.path, "isjunction") and os.path.isjunction(cur)):
                raise PolicyViolation(f"symlink/junction in the path: {rel}")

    def write_atomic(self, path, data: bytes) -> list[str]:
        """Write bytes as one atomic step: exclusive temp file in the target folder -> fsync -> os.replace.
        Path checks are repeated immediately before and after the write; they narrow, but cannot close, a race with
        another local process that swaps a folder for a link in between (see README). Returns folders it created."""
        p, rel = self.policy.check_path(path, "write")
        if len(data) > self.policy.max_write:
            raise PolicyViolation("write exceeds max_file_write_bytes")
        self._no_link_on_path(rel)
        if p.is_dir():
            raise PolicyViolation("target is a directory")
        created, cur = [], self.policy.root
        for part in rel.split("/")[:-1]:
            cur = cur / part
            if not cur.exists():
                cur.mkdir()
                created.append(os.path.relpath(cur, self.policy.root).replace("\\", "/"))
        self._no_link_on_path(rel)
        if self.policy.check_path(path, "write")[1] != rel:
            raise PolicyViolation(f"target path changed during the write: {rel}")
        tmp = p.parent / f".{p.name}.orchtmp{os.getpid()}-{os.urandom(4).hex()}"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o644)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._no_link_on_path(rel)
        return created

    def read(self, path) -> str:
        p, _ = self.policy.check_path(path, "read")
        return p.read_text(encoding="utf-8")

    def delete(self, path) -> None:
        p, rel = self.policy.check_path(path, "delete")
        if not p.is_file():
            raise PolicyViolation(f"delete target is not an existing file: {rel}")
        if self.policy.deletes_require_approval and ("delete", rel) not in self.policy.approved:
            raise ApprovalRequired("delete", rel)
        p.unlink()

    def run(self, argv) -> ProcResult:
        self.policy.check_command(argv)
        return run_process(argv, cwd=self.policy.root, env=self.env, timeout=self.timeout)


# --------------------------------------------------------------------------- evidence
def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    try:
        if os.path.islink(path):
            return "symlink:" + os.readlink(path)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return "unreadable"
    return h.hexdigest()


def snapshot(root, ignore_dirs, max_files: int = 20000) -> dict[str, str]:
    """relative posix path -> sha256, for every file (incl. untracked and protected ones)."""
    out: dict[str, str] = {}
    ignore = set(ignore_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for name in filenames:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, root).replace("\\", "/")] = _hash_file(full)
            if len(out) > max_files:
                raise RuntimeError("workspace too large for snapshot (max_files exceeded)")
    return out


def diff_snapshots(a: dict, b: dict) -> dict[str, list[str]]:
    return {"added": sorted(set(b) - set(a)), "deleted": sorted(set(a) - set(b)),
            "modified": sorted(k for k in set(a) & set(b) if a[k] != b[k])}


def snapshot_digest(s: dict) -> str:
    return hashlib.sha256("\n".join(f"{k}\0{v}" for k, v in sorted(s.items())).encode()).hexdigest()


def git_status(root, env) -> list[str] | None:
    """Read-only extra evidence if the workspace is a git repo (includes untracked files)."""
    if not (Path(root) / ".git").exists() or not shutil.which("git"):
        return None
    try:
        r = run_process(["git", "status", "--porcelain=v1", "-uall"], cwd=root, env=env, timeout=20)
    except (ProcessTimeout, ProcessCancelled, OSError):
        return None
    return r.stdout.splitlines() if r.returncode == 0 else None


_TESTS_RAN = [re.compile(r"\bRan (\d+) tests?\b"), re.compile(r"\b(\d+) passed\b")]


def tests_ran(output: str) -> int | None:
    for pat in _TESTS_RAN:
        m = pat.search(output)
        if m:
            return int(m.group(1))
    return None


_QUOTA = re.compile(r"usage limit|rate.?limit|quota|too many requests|\b429\b|limit reached|"
                    r"credit balance|insufficient credit|billing|5-hour limit|weekly limit|limit will reset|"
                    r"out of extra usage|reached your .{0,30}limit", re.I)
_AUTH = re.compile(r"not logged in|log ?in required|please (?:run|log)|unauthori[sz]ed|\b401\b|"
                   r"invalid api key|token expired|authentication|/login|oauth token has expired|invalid authentication", re.I)


def classify_cli_error(text: str) -> str | None:
    """Map CLI stderr/stdout to 'quota' / 'auth' / None. For future real adapters."""
    if _QUOTA.search(text):
        return "quota"
    if _AUTH.search(text):
        return "auth"
    return None
