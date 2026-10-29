"""Simulated Claude Code CLI (NO real Claude involved). Used as the injectable `runner` of claude_adapter.ClaudeCli.

It behaves like the CLI as far as the adapter can observe: answers --version / auth status / --help, parses the
-p command line, refuses forbidden flags, tracks sessions (--session-id creates, --resume must exist), reads the
prompt from stdin, optionally edits files in its working directory (to simulate the agent working), and returns
a JSON envelope. The envelope SHAPE is an assumption (see claude_adapter.LIMITATIONS), not an observation.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from claude_adapter import FORBIDDEN_FLAGS, REQUIRED_FLAGS, UUID_RE
from safety import ProcResult


class SimClaude:
    simulated = True  # no model behind it: run_policy treats its (scripted) output as trusted test fixtures
    def __init__(self, steps=(), *, version="2.1.293 (Claude Code)", auth=None, help_missing=(), uuid_factory=None):
        self.steps, self.calls, self.prompts, self.sessions = list(steps), [], [], {}
        self.gate_runs = []
        self.version = version
        self.auth = auth if auth is not None else {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                                                   "subscriptionType": "pro"}
        self.help_missing = set(help_missing)
        self.uuid_factory = uuid_factory or uuid.uuid4

    # ------------------------------------------------------------------ dispatch
    def __call__(self, argv, **kw):
        self.calls.append({"argv": list(argv), "kw": kw})
        args = list(argv[1:])
        if args == ["--version"]:
            return ProcResult(0, self.version + "\n", "", 0.1)
        if args == ["auth", "status"]:
            return ProcResult(0, json.dumps(self.auth), "", 0.1)
        if args == ["--help"]:
            flags = [f for f in REQUIRED_FLAGS if f not in self.help_missing]
            extra = ("  --effort <level>    Effort level for the current session\n                      (low, medium, high, xhigh, max)\n"
                     "  --model <model>     Model for the current session. Provide\n                      an alias for the latest model (e.g.\n"
                     "                      'fable', 'opus', or 'sonnet') or a\n                      model's full name.\n"
                     "  -n, --name <name>   (simulated)\n")
            return ProcResult(0, "Options:\n" + "\n".join(f"  {f}  (simulated)" for f in flags) + "\n" + extra, "", 0.1)
        assert "-p" in args, f"unexpected simulated command: {args}"
        bad = [f for f in FORBIDDEN_FLAGS if f in args]
        assert not bad, f"forbidden flags reached the CLI: {bad}"
        return self._print_mode(args, kw)

    def _print_mode(self, args, kw):
        def opt(name):
            return args[args.index(name) + 1] if name in args else None
        prompt = kw.get("stdin_text")
        self.prompts.append(prompt)
        step = self.steps.pop(0) if self.steps else {"report": {"summary": "nothing scripted", "changed_files": [],
                                                                  "commands_run": [], "remaining_issues": []}}
        if "raise" in step:
            raise step["raise"]
        sid = opt("--session-id")
        if sid:
            assert UUID_RE.match(sid), "session id must be a UUID"
            assert sid not in self.sessions, "session id reused for a new session"
            self.sessions[sid] = []
        else:
            sid = opt("--resume")
            if sid not in self.sessions:
                return ProcResult(1, json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                                                 "result": f"No conversation found with session ID: {sid}"}), "", 0.1)
        self.sessions[sid].append(prompt)
        cwd = Path(kw["cwd"])
        denials = []
        for op in step.get("ops", []):  # simulate the agent working in its cwd (deliberately unchecked)
            if op["op"] == "capgate":  # Claude runs the capability gateway through its Bash tool
                from claude_adapter import CAPGATE_RULE, _GATE
                allowed = opt("--allowedTools") or ""
                if not CAPGATE_RULE or CAPGATE_RULE not in allowed.split(","):
                    denials.append({"tool_name": "Bash", "tool_input": {"command": "python -I " + _GATE + " " + " ".join(op["args"])}})
                    continue
                import subprocess
                import sys
                r = subprocess.run([sys.executable, "-I", _GATE, *op["args"]], cwd=str(cwd), env=kw.get("env"),
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                self.gate_runs.append({"args": op["args"], "rc": r.returncode, "out": r.stdout})
                continue
            if op["op"] == "bash":  # a raw shell command: only the gateway prefix is allowed in these simulations
                denials.append({"tool_name": "Bash", "tool_input": {"command": op["command"]}})
                continue
            target = (cwd / op["path"]).resolve()
            if op["op"] == "write":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(op["content"], encoding="utf-8", newline="\n")
            elif op["op"] == "delete":
                os.remove(target)
        report = step.get("report", {"summary": "done", "changed_files": [o["path"] for o in step.get("ops", []) if "path" in o],
                                     "commands_run": ["python -m unittest discover -s tests"], "remaining_issues": []})
        env = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 3200, "duration_api_ms": 2900,
               "num_turns": step.get("num_turns", 3), "result": json.dumps(report), "structured_output": report,
               "session_id": sid, "total_cost_usd": 0.0123, "permission_denials": [],
               "usage": {"input_tokens": 1200, "output_tokens": 340}}
        if step.get("result_text_only"):
            env.pop("structured_output")
        if denials:
            env["permission_denials"] = denials
        env.update(step.get("envelope", {}))
        if "stdout_raw" in step:
            return ProcResult(step.get("rc", 0), step["stdout_raw"], step.get("stderr", ""), 3.2)
        return ProcResult(step.get("rc", 0), json.dumps(env), step.get("stderr", ""), 3.2)
