"""AI-CLI shim: put FIRST on PATH (as codex/claude wrappers) while an owner-declared AI-integration script runs through the
capability gateway. Every `codex exec` / `claude -p` the product makes passes through here and is enforced IN CODE:

* counted against the task's owner-authorized allowance (atomic, across processes) - beyond it: refused, exit 3;
* the model must be one the owner authorized for this task; with no model given, the first authorized one is set;
* dangerous flags (sandbox/permission bypasses, a service tier) are refused;
* every call is written to the task's audit log (`ai_cli_run`), so the orchestrator's budget and the owner see it.

Non-inference subcommands (version, login status, features list, help) pass through uncounted. The real CLI is the one
found on PATH without this shim. Standard library only (runs under `python -I`). HONEST SCOPE: a program that starts the
CLI by an absolute path instead of PATH is not intercepted; the gateway's screening and audit are the other layers.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

EXIT_REFUSED = 3
_BYPASS = ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--dangerously-skip-permissions",
           "--allow-dangerously-skip-permissions", "danger-full-access", "bypassPermissions", "service_tier", "--full-auto")


def _inference(tool: str, args: list) -> bool:
    if tool == "claude":
        return "-p" in args or "--print" in args
    # any position: option VALUES (e.g. `-m <model> exec`) must not hide the subcommand; counting too much is the safe side
    return any(a in ("exec", "e", "review") for a in args)


def _model(args: list) -> str | None:
    for i, a in enumerate(args):
        if a in ("-m", "--model") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    return None


class _Lock:
    """Cross-process lock via an atomic mkdir (works on Windows and POSIX)."""
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        for _ in range(600):
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                time.sleep(0.05)
        raise SystemExit("ai-shim: lock timeout")

    def __exit__(self, *a):
        try:
            os.rmdir(self.path)
        except OSError:
            pass


def _audit(pol: dict, rec: dict) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "task_id": pol.get("task_id"), "requested_by": "project script",
           "capability": "ai_cli", **rec}
    with open(pol["audit"], "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _used(pol: dict) -> int:
    n = 0
    try:
        with open(pol["audit"], encoding="utf-8") as f:
            for ln in f:
                try:
                    n += bool(json.loads(ln).get("ai_cli_run"))
                except ValueError:
                    continue
    except OSError:
        pass
    return n


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    tool, args = (argv[0] if argv else ""), argv[1:]
    try:
        pol = json.loads(open(os.environ["ORCH_AI_SHIM"], encoding="utf-8").read())
    except (KeyError, OSError, ValueError):
        print("ai-shim: no orchestrator policy; refusing", file=sys.stderr)
        return EXIT_REFUSED
    if tool not in ("codex", "claude"):
        return EXIT_REFUSED
    real = shutil.which(tool, path=pol.get("real_path") or "")
    if not real:
        print(f"ai-shim: the real {tool} CLI was not found", file=sys.stderr)
        return 127
    bad = [a for a in args if any(b in a for b in _BYPASS)]
    if bad:
        _audit(pol, {"tool": tool, "decision": "refused", "reason": f"forbidden flag(s): {bad[:3]}"})
        print(f"ai-shim: refused - forbidden flag(s) {bad[:3]} (orchestrator policy)", file=sys.stderr)
        return EXIT_REFUSED
    if _inference(tool, args):
        allowed_models = (pol.get("models") or {}).get(tool) or []
        m = _model(args)
        if m and m not in allowed_models:
            _audit(pol, {"tool": tool, "model": m, "decision": "refused", "reason": "model not authorized for this task"})
            print(f"ai-shim: refused - model {m!r} is not authorized for this task; authorized: {allowed_models or 'CLI default'}",
                  file=sys.stderr)
            return EXIT_REFUSED
        if not m and allowed_models:
            m = allowed_models[0]
            args = (["-m", m] if tool == "codex" else []) + args + (["--model", m] if tool == "claude" else [])
        with _Lock(pol["audit"] + ".lock"):
            used, limit = _used(pol), int(pol.get("allowed_runs") or 0)
            if used >= limit:
                _audit(pol, {"tool": tool, "model": m, "decision": "refused", "reason": f"allowance used ({used}/{limit})"})
                print(f"ai-shim: refused - the owner's allowance of {limit} AI CLI call(s) for this task is used up", file=sys.stderr)
                return EXIT_REFUSED
            _audit(pol, {"tool": tool, "model": m or "CLI default", "decision": "allowed", "ai_cli_run": True,
                         "reason": f"AI CLI call {used + 1} of {limit} (owner-authorized, metered)"})
    return subprocess.call([real, *args])


if __name__ == "__main__":
    sys.exit(main())
