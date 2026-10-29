"""Capability gateway: the ONE command Claude may run for routine verification, with structured arguments instead of
composed shell strings (cd && ... | tail; echo $?; for-loops), which the Claude Code permission rules deny.

    python -I <orchestrator>/capgate.py test   [--target T ...] [--discover DIR] [--pattern GLOB] [--runner unittest|pytest]
                                               [--keyword K] [--repeat N] [--failfast] [--verbose] [--tail N] [--grep REGEX]
    python -I <orchestrator>/capgate.py script REL/PATH.py [--arg VALUE ...] [--isolated] [--tail N] [--grep REGEX]
    python -I <orchestrator>/capgate.py env    [--tool NAME ...] [--module NAME ...]

Every request is validated against the task's policy file (written by the orchestrator OUTSIDE the project from the
owner-authorized task state; its path comes from the ORCH_CAP_POLICY environment variable of the Claude process), run
with an explicit argument list (no shell), in the authorized project root, with a timeout, and recorded in the task's
audit log (who, what, why allowed/denied, exit code). Exit codes: the command's own code, 2 invalid request,
3 OWNER APPROVAL REQUIRED, 4 denied by policy, 5 no policy.

HONEST SCOPE: tests and scripts run as ordinary processes under the owner's Windows account - the same as
`python -m unittest`, which was already allowed. This gateway removes shell composition, confines arguments and working
directory to the project, screens scripts for external effects and audits everything. It is NOT an OS sandbox.
Standard library only: it runs under `python -I`, so it cannot import the orchestrator's modules.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

EXIT_INVALID, EXIT_OWNER, EXIT_DENIED, EXIT_NO_POLICY = 2, 3, 4, 5
MAX_REPEAT = 5
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./\\:\[\]-]{0,199}$")
_SAFE_WORD = re.compile(r"^[A-Za-z0-9_.*?\[\]-]{1,100}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
_KEYWORD = re.compile(r"^[A-Za-z0-9_ ()-]{1,120}$")
# Screening for EXTERNAL effects or destruction (never containment). It reads the CODE (Python AST: real imports, calls and
# string constants - not comments or docstrings) of the script and of every project module it imports, transitively.
NET_MODULES = {"socket", "urllib", "http", "httpx", "aiohttp", "requests", "smtplib", "ftplib", "paramiko", "websocket",
               "websockets", "webbrowser", "boto3", "openai", "anthropic", "telnetlib", "xmlrpc", "ssl", "imaplib", "poplib"}
SYS_MODULES = {"ctypes", "winreg", "_winapi", "msvcrt"}
PROC_MODULES = {"subprocess", "pty", "multiprocessing"}
AGENT_NAMES = {"claude", "codex", "claude.cmd", "codex.cmd", "claude.exe", "codex.exe"}
DANGER_EXES = {"git", "curl", "wget", "powershell", "pwsh", "cmd", "cmd.exe", "rm", "del", "rmdir", "scp", "ssh", "ftp", "pip",
               "pip3", "npm", "npx", "winget", "choco", "reg", "regedit", "setx", "shutdown", "taskkill", "icacls", "takeown",
               "netsh", "schtasks", "gh", "vercel", "netlify", "docker"}
_OS_PROC = {"system", "popen", "startfile", "execv", "execve", "execl", "execlp", "execvp", "spawnl", "spawnv", "spawnve", "posix_spawn"}
_OS_DEL = {"remove", "unlink", "rmdir", "removedirs"}
# kinds a script the OWNER DECLARED as an AI-integration entry point may have: its AI CLI calls are metered by aishim.py and
# deletions inside the project are detected afterwards by the orchestrator. network/system/dangerous commands never pass.
DECLARED_OK = {"processes", "agent_cli", "deletion", "dynamic_code"}


def analyze(src: str) -> tuple[set, list]:
    """(kinds, imports) of one Python file; imports are (module, level, names)."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {"unparseable"}, []
    kinds, imports = set(), []

    def mod_kind(name):
        root = (name or "").split(".")[0]
        if root in NET_MODULES:
            kinds.add("network")
        elif root in SYS_MODULES:
            kinds.add("system")
        elif root in PROC_MODULES:
            kinds.add("processes")
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                mod_kind(a.name)
                imports.append((a.name, 0, []))
        elif isinstance(n, ast.ImportFrom):
            if not n.level:
                mod_kind(n.module)
            imports.append((n.module or "", n.level, [a.name for a in n.names]))
        elif isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                owner, attr = f.value.id, f.attr
                if owner == "os" and attr in _OS_PROC:
                    kinds.add("processes")
                elif (owner == "os" and attr in _OS_DEL) or (owner == "shutil" and attr == "rmtree"):
                    kinds.add("deletion")
                elif owner == "importlib" and attr == "import_module":
                    kinds.add("dynamic_code")
            elif isinstance(f, ast.Attribute) and f.attr in ("unlink", "rmdir", "rmtree"):
                kinds.add("deletion")
            elif isinstance(f, ast.Name) and f.id in ("exec", "eval", "__import__"):
                kinds.add("dynamic_code")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.strip().lower() in AGENT_NAMES:
            kinds.add("agent_cli")
    # a dangerous program counts only where it is LAUNCHED: the head of a command list/tuple, or the first word of a command
    # string given to subprocess/os.system (a path segment such as "WinGet" in a folder name is not a command)
    heads = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.List, ast.Tuple)) and n.elts and isinstance(n.elts[0], ast.Constant) and isinstance(n.elts[0].value, str):
            heads.append(n.elts[0].value)
        elif (isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
              and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
              and (n.func.value.id, n.func.attr) in {("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
                                                    ("subprocess", "check_call"), ("subprocess", "check_output"),
                                                    ("os", "system"), ("os", "popen")}):
            heads.append(n.args[0].value)
    for h in heads:
        exe = os.path.basename(h.strip().split(" ")[0].strip('"')).lower() if h.strip() else ""
        if exe in DANGER_EXES or exe.rsplit(".", 1)[0] in DANGER_EXES:
            kinds.add("consequential_command")
    return kinds, imports


def _out(text: str) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    print(text, flush=True)


class Gate:
    def __init__(self, policy: dict):
        self.p = policy
        self.root = Path(os.path.realpath(policy["root"]))
        self.protected = [x.lower() for x in policy.get("protected") or []]
        self.raw: list = []  # the request exactly as given (for the audit log)

    # ------------------------------------------------------------------ audit
    def audit(self, cap: str, args: list, decision: str, reason: str, **extra) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "task_id": self.p.get("task_id"), "cycle": self.p.get("cycle"),
               "requested_by": "claude", "capability": cap, "args": [str(a)[:300] for a in args][:40], "decision": decision,
               "reason": reason[:600], **extra}
        try:
            with open(self.p["audit"], "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ paths
    def inside(self, rel: str) -> Path:
        """A project-relative path that must stay inside the root and must not be protected."""
        if "\x00" in rel or not rel.strip():
            raise ValueError("empty path")
        p = Path(rel)
        real = Path(os.path.realpath(p if p.is_absolute() else self.root / p))
        try:
            ok = os.path.commonpath([os.path.normcase(str(self.root)), os.path.normcase(str(real))]) == os.path.normcase(str(self.root))
        except ValueError:
            ok = False
        if not ok:
            raise ValueError(f"path outside the authorized project: {rel}")
        r = os.path.relpath(real, self.root).replace("\\", "/")
        if any(fnmatch.fnmatch(part.lower(), pat) for part in r.split("/") for pat in self.protected) or \
                any(fnmatch.fnmatch(r.lower(), pat) for pat in self.protected):
            raise ValueError(f"protected path: {r}")
        return real

    # ------------------------------------------------------------------ process
    def run(self, argv: list, timeout: int, extra_env: dict | None = None) -> tuple[int | None, str, float]:
        env = {k: v for k, v in os.environ.items() if k != "ORCH_CAP_POLICY"}
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(extra_env or {})
        t0 = time.time()
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        p = subprocess.Popen(argv, cwd=str(self.root), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, creationflags=flags, start_new_session=os.name != "nt")
        try:
            out, _ = p.communicate(timeout=timeout)
            code = p.returncode
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=15)
            p.kill()
            out, _ = p.communicate()
            code = None
        return code, (out or b"").decode("utf-8", "replace"), time.time() - t0

    @staticmethod
    def shape(text: str, tail: int, grep: str | None) -> str:
        lines = text.splitlines()
        if grep:
            rx = re.compile(grep)
            lines = [ln for ln in lines if rx.search(ln)]
        return "\n".join(lines[-tail:])

    # ------------------------------------------------------------------ capabilities
    def test(self, a) -> int:
        args = self.raw
        targets = []
        for t in a.target or []:
            if not _SAFE_TARGET.match(t) or t.startswith("-"):
                raise ValueError(f"invalid test target: {t!r}")
            if "/" in t or "\\" in t or t.endswith(".py"):
                self.inside(t.split("::")[0])
            targets.append(t)
        if a.discover and a.discover != ".":  # a test folder must be inside the project, for both runners
            if not _SAFE_TARGET.match(a.discover):
                raise ValueError("invalid --discover")
            self.inside(a.discover)
        if a.runner == "pytest":
            argv = [sys.executable, "-m", "pytest", "-q", *(["-x"] if a.failfast else []), *(["-v"] if a.verbose else [])]
            if a.keyword:
                if not _KEYWORD.match(a.keyword):
                    raise ValueError("invalid --keyword")
                argv += ["-k", a.keyword]
            argv += targets or [a.discover or "."]
        else:
            argv = [sys.executable, "-m", "unittest", *(["-f"] if a.failfast else []), *(["-v"] if a.verbose else [])]
            if targets:
                argv += targets
            else:
                d = a.discover or ("tests" if (self.root / "tests").is_dir() else ".")
                self.inside(d) if d != "." else None
                argv += ["discover", "-s", d, *(["-p", a.pattern] if a.pattern else [])]
            if a.pattern and not _SAFE_WORD.match(a.pattern):
                raise ValueError("invalid --pattern")
        timeout = int(self.p.get("test_timeout") or 300)
        results, worst = [], 0
        for i in range(1, a.repeat + 1):
            code, out, dur = self.run(argv, timeout)
            ran = _num(r"\bRan (\d+) tests?\b|\b(\d+) passed\b", out)
            skipped = _num(r"skipped=(\d+)|\b(\d+) skipped\b", out) or 0
            results.append({"run": i, "exit_code": code, "tests_ran": ran, "skipped": skipped, "duration_s": round(dur, 1)})
            _out(f"--- run {i}/{a.repeat}: exit {code if code is not None else 'TIMEOUT'}, tests ran {ran}, skipped {skipped}, "
                 f"{dur:.1f}s ---")
            _out(self.shape(out, a.tail, a.grep))
            if code != 0 and worst == 0:
                worst = 124 if code is None else code
        _out(f"CAPABILITY_RESULT test exit={worst} runs={a.repeat} passed_runs={sum(r['exit_code'] == 0 for r in results)} "
             f"skipped_total={sum(r['skipped'] for r in results)} (skipped tests are NOT evidence)")
        self.audit("test", args, "allowed", "project tests (class 1)", exit_code=worst, runs=results, argv=argv[1:])
        return worst

    def _resolve(self, path: Path, mod: str, level: int, names: list) -> list[Path]:
        if level:
            base = path.parent
            for _ in range(level - 1):
                base = base.parent
            target = base / mod.replace(".", "/") if mod else base
        else:
            target = self.root / mod.replace(".", "/")
        cands = [target.with_suffix(".py"), target / "__init__.py"]
        cands += [c for nm in names for c in (target / f"{nm}.py", target / nm / "__init__.py")]
        out = []
        for c in cands:
            try:
                inside = c.is_file() and os.path.commonpath([os.path.normcase(str(self.root)),
                                                             os.path.normcase(os.path.realpath(c))]) == os.path.normcase(str(self.root))
            except ValueError:
                inside = False
            if inside:
                out.append(Path(os.path.realpath(c)))
        return out

    def screen(self, path: Path) -> list[str]:
        """Kinds found in the script and in every project module it imports (transitively, at most 60 files)."""
        todo, seen, found = [Path(os.path.realpath(path))], set(), []
        while todo and len(seen) < 60:
            f = todo.pop(0)
            if f in seen:
                continue
            seen.add(f)
            try:
                src = f.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise ValueError(f"cannot read script: {e}")
            kinds, imports = analyze(src)
            rel = os.path.relpath(f, self.root).replace(os.sep, "/")
            found += [f"{k} ({rel})" for k in sorted(kinds)]
            for mod, level, names in imports:
                todo += [c for c in self._resolve(f, mod, level, names) if c not in seen]
        if todo:
            found.append("too_many_modules (screening incomplete)")
        return found

    def shim_env(self) -> dict:
        """PATH with the orchestrator's AI-CLI shim first (aishim.py meters and enforces every codex/claude call)."""
        shim = self.p.get("shim_dir")
        if not shim or not Path(shim).is_dir() or not self.p.get("shim_policy"):
            raise ValueError("the AI-CLI shim is not set up for this task")
        return {"PATH": shim + os.pathsep + os.environ.get("PATH", ""), "ORCH_AI_SHIM": self.p["shim_policy"]}

    def script(self, a) -> int:
        args = self.raw
        path = self.inside(a.path)
        if path.suffix.lower() != ".py" or not path.is_file():
            raise ValueError("script must be an existing .py file inside the project")
        rel = os.path.relpath(path, self.root).replace("\\", "/")
        for v in a.arg or []:
            if "\x00" in v or len(v) > 300:
                raise ValueError("invalid --arg")
            val = v.split("=", 1)[1] if v.startswith("--") and "=" in v else v
            if re.match(r"^([A-Za-z]:[\\/]|[\\/]{1,2}\w|~)", val) or ".." in val.replace("\\", "/").split("/"):
                self.inside(val)  # a path argument must stay inside the project
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        approved = any(x.get("path") == rel and x.get("sha256") == sha for x in self.p.get("approved_scripts") or [])
        findings = self.screen(path)
        kinds = {f.split(" ")[0] for f in findings}
        declared = rel in (self.p.get("ai_scripts") or [])
        extra_env = None
        if approved:
            reason = "approved by the owner for this task (same file content)"
            if "agent_cli" in kinds and self.p.get("shim_policy"):
                extra_env = self.shim_env()  # its AI CLI calls are metered like a declared script's
        elif declared and self.p.get("scripts") == "screened" and kinds <= DECLARED_OK:
            extra_env = self.shim_env()  # its AI CLI calls are metered and model-checked in code by aishim.py
            reason = ("owner-declared AI-integration script (mission setup): AI CLI calls metered by the orchestrator shim "
                      f"({self.p.get('ai_runs_allowed', 0)} allowed for the task, authorized models only)"
                      + (f"; also found: {', '.join(sorted(kinds - {'agent_cli'}))}" if kinds - {"agent_cli"} else ""))
        elif self.p.get("scripts") == "screened" and not findings:
            reason = "screened: no network, process, deletion, system or agent-CLI use found (class 2, autonomy level allows)"
        else:
            blocking = sorted(kinds - DECLARED_OK) if declared else []
            why = (("declared AI-integration script, but found: " + ", ".join(blocking)) if blocking else
                   ("found: " + ", ".join(findings)) if findings else "the owner chose supervised autonomy")
            self.audit("script", args, "needs_owner", why, script={"path": rel, "sha256": sha}, findings=findings, declared=declared)
            _out(f"OWNER_APPROVAL_REQUIRED: script {rel} (sha256 {sha[:12]}) was NOT run - {why}. The orchestrator asks the "
                 "owner after this step. Do not try another way to run it; report it in remaining_issues and continue other work.")
            return EXIT_OWNER
        argv = [sys.executable, *(["-I"] if a.isolated else []), str(path), *(a.arg or [])]
        code, out, dur = self.run(argv, int(self.p.get("script_timeout") or 600), extra_env)
        _out(self.shape(out, a.tail, a.grep))
        _out(f"CAPABILITY_RESULT script {rel} exit={code if code is not None else 'TIMEOUT'} ({dur:.1f}s)")
        self.audit("script", args, "allowed", reason, script={"path": rel, "sha256": sha}, findings=findings,
                   exit_code=code, duration_s=round(dur, 1), declared=declared)
        return 124 if code is None else code

    def results(self, a) -> int:
        """Read-only: results of the orchestrator's typed agent operations (e.g. Codex reviews of saved artifacts)."""
        try:
            ops = json.loads(Path(self.p.get("agent_ops") or "").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ops = []
        if a.id:
            ops = [o for o in ops if o.get("id") == a.id]
        _out(json.dumps(ops[-a.last:], ensure_ascii=False, indent=1) if ops else "no agent operation results yet")
        self.audit("results", self.raw, "allowed", "read-only results of orchestrator agent operations (class 1)", exit_code=0)
        return 0

    def env(self, a) -> int:
        args = self.raw
        tools = a.tool or ["ffmpeg", "ffprobe", "node", "npm", "git"]
        mods = a.module or []
        for n in tools + mods:
            if not _SAFE_NAME.match(n):
                raise ValueError(f"invalid name: {n!r}")
        lines = [f"project root: {self.root}", f"python: {sys.executable}"]
        lines += [f"tool {t}: {shutil.which(t) or 'NOT FOUND'}" for t in tools]
        if mods:  # checked by the normal interpreter in the project (this gateway runs isolated)
            code = ("import importlib.util,json,sys;print(json.dumps({m:bool(importlib.util.find_spec(m)) for m in sys.argv[1:]}))")
            rc, out, _ = self.run([sys.executable, "-c", code, *mods], 60)
            try:
                found = json.loads(out.strip().splitlines()[-1])
            except (ValueError, IndexError):
                found = {}
            lines += [f"module {m}: {'available' if found.get(m) else 'NOT AVAILABLE'}" for m in mods]
        _out("\n".join(lines))
        self.audit("env", args, "allowed", "read-only diagnostics (class 1)", exit_code=0)
        return 0


def _num(rx: str, text: str):
    m = re.search(rx, text)
    return int(next(g for g in m.groups() if g)) if m else None


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="capgate", description="structured, audited project operations")
    sub = ap.add_subparsers(dest="cap", required=True)
    t = sub.add_parser("test")
    t.add_argument("--runner", choices=("unittest", "pytest"), default="unittest")
    t.add_argument("--target", action="append")
    t.add_argument("--discover")
    t.add_argument("--pattern")
    t.add_argument("--keyword")
    t.add_argument("--repeat", type=int, default=1, choices=range(1, MAX_REPEAT + 1))
    t.add_argument("--failfast", action="store_true")
    t.add_argument("--verbose", action="store_true")
    t.add_argument("--tail", type=int, default=60, choices=range(1, 401), metavar="N")
    t.add_argument("--grep")
    s = sub.add_parser("script")
    s.add_argument("path")
    s.add_argument("--arg", action="append")
    s.add_argument("--isolated", action="store_true")
    s.add_argument("--tail", type=int, default=120, choices=range(1, 401), metavar="N")
    s.add_argument("--grep")
    r = sub.add_parser("results")
    r.add_argument("--id")
    r.add_argument("--last", type=int, default=5, choices=range(1, 51), metavar="N")
    e = sub.add_parser("env")
    e.add_argument("--tool", action="append")
    e.add_argument("--module", action="append")
    return ap


def main(argv=None) -> int:
    ap = parser()
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return EXIT_INVALID
    pol_path = os.environ.get("ORCH_CAP_POLICY")
    try:
        policy = json.loads(Path(pol_path).read_text(encoding="utf-8")) if pol_path else None
    except (OSError, ValueError):
        policy = None
    if not policy or not Path(policy.get("root", "")).is_dir():
        _out("DENIED: no capability policy for this process (only usable inside an orchestrator task).")
        return EXIT_NO_POLICY
    g = Gate(policy)
    g.raw = list(argv if argv is not None else sys.argv[1:])[1:]
    if getattr(a, "grep", None):
        try:
            re.compile(a.grep)
        except re.error:
            _out("INVALID: --grep is not a valid regular expression")
            return EXIT_INVALID
        if len(a.grep) > 200:
            return EXIT_INVALID
    try:
        return {"test": g.test, "script": g.script, "env": g.env, "results": g.results}[a.cap](a)
    except ValueError as e:
        g.audit(a.cap, g.raw, "denied", str(e))
        _out(f"DENIED: {e}")
        return EXIT_DENIED


if __name__ == "__main__":
    sys.exit(main())
