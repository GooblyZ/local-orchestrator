"""Command-line interface (run / status / pause / resume / stop / approve / conversation / replay / watch ...)."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from agents import AdapterError, LiveNotEnabled, make_mock_agents
from config import ConfigError, HERE, OrchestratorError, load_config, resolve_runs_dir
from conversation import Transcript, read_transcript
from safety import Redactor, scrub_env
from store import (ActiveTaskExists, EventLog, WorkspaceLocked, atomic_write_json, read_json, write_control)
from taskctl import dry_run, format_status, resolve_approval, resolve_task
from viewer import Viewer, replay, select, watch
import web_viewer


def build_agents(a, cfg, adapters, codex_runner, codex_base_dir=None, claude_runner=None, claude_which=None):
    """mock = all mock; hybrid = real Codex planner/reviewer (gated) + mock executor;
    live = real Codex + real Claude executor (each role gated by its own approval key)."""
    mock = adapters or make_mock_agents(read_json(a.scenario) if a.scenario else {})
    if a.agents in ("hybrid", "live") and adapters is None:
        from codex_adapter import live_gate, make_codex_agents
        kw = {"base_dir": codex_base_dir} if codex_base_dir else {}
        if a.agents == "live":
            live_gate(cfg, "claude_executor")
        planner, reviewer, _ = make_codex_agents(cfg, runner=codex_runner, **kw)
        if a.agents == "hybrid":
            return planner, mock[1], reviewer
        from claude_adapter import ClaudeCli, ClaudeExecutor
        ck = {"which": claude_which} if claude_which else {}
        return planner, ClaudeExecutor(ClaudeCli(cfg, runner=claude_runner, **ck)), reviewer
    return mock


def main(argv=None, out=print, adapters=None, codex_runner=None, codex_base_dir=None, claude_runner=None,
         claude_which=None, inp=input, tty=None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator.py", description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(HERE / "config.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--project", required=True); r.add_argument("--task", required=True)
    r.add_argument("--agents", choices=["mock", "hybrid", "live"], default="mock"); r.add_argument("--scenario")
    r.add_argument("--dry-run", action="store_true"); r.add_argument("--max-cycles", type=int)
    r.add_argument("--allow-create", action="append", default=[], metavar="PATH",
                   help="proposal mode (Phase 4d): a relative path the executor may PROPOSE; the orchestrator writes it")
    r.add_argument("--allow-overwrite", action="append", default=[], metavar="PATH", help="proposal mode: an existing file that may be replaced")
    r.add_argument("--require-def", action="append", default=[], metavar="NAME", help="proposal mode: required top-level definition")
    r.add_argument("--accept-local-execution", action="store_true",
                   help="NATIVE profile: Claude Code edits files and runs the project's tests on this machine under your permissions")
    for q in (r, ):
        q.add_argument("--quiet", action="store_true", help="do not print the conversation (it is still saved)")
    for name in ("status", "pause", "resume", "stop"):
        p = sub.add_parser(name); p.add_argument("--task-id")
        if name == "status":
            p.add_argument("--json", action="store_true")
        if name == "resume":
            p.add_argument("--scenario"); p.add_argument("--agents", choices=["mock", "hybrid", "live"], default="mock")
            p.add_argument("--quiet", action="store_true")
    at = sub.add_parser("allow-tool", help="decide on Claude's pending permission request (this task only), then resume")
    at.add_argument("--task-id"); at.add_argument("--rule", action="append", default=[], help="e.g. \"Bash(npm test)\" (default: the suggested rule)")
    at.add_argument("--deny", action="store_true")
    eb = sub.add_parser("extend-budget", help="goal mode: raise a paused task's budget (owner only), then resume")
    eb.add_argument("--task-id")
    for k in ("cycles", "minutes", "codex-calls", "claude-calls"):
        eb.add_argument(f"--{k}", type=int, default=0)
    ap_ = sub.add_parser("approve"); ap_.add_argument("--task-id"); ap_.add_argument("--approval-id", required=True)
    ap_.add_argument("--deny", action="store_true")
    sub.add_parser("preflight"); sub.add_parser("codex-preflight")
    sub.add_parser("claude-preflight")
    dm = sub.add_parser("demo"); dm.add_argument("--quiet", action="store_true")
    dm.add_argument("--claude-sim", action="store_true", help="use the REAL Claude adapter against a simulated CLI")
    dm.add_argument("--proposals", action="store_true", help="MOCK example of controlled file creation (Phase 4d; no model is called)")
    dm.add_argument("--he-summaries", action="store_true", help="MOCK example of agent-written Hebrew summaries (no model is called)")
    dm.add_argument("--delay", type=float, default=0.0, help="seconds each mock agent 'thinks' (so you can watch the browser update)")
    sub.add_parser("app", help="the browser app: enter a task, approve, watch (what start.cmd runs)")
    st_ = sub.add_parser("start", help="run a REAL Codex+Claude task safely (asks for the task, confirmation in this terminal)")
    st_.add_argument("--task"); st_.add_argument("--project", help="existing folder (default: a new folder under workspaces/)")
    st_.add_argument("--task-file", help="read a long task (UTF-8) from a file instead of the command line")
    st_.add_argument("--allow", action="append", default=[], help="file Claude may create (repeat, or comma-separated; patterns like tests/*.py)")
    st_.add_argument("--allow-overwrite", action="append", default=[]); st_.add_argument("--require-def", action="append", default=[])
    st_.add_argument("--max-cycles", type=int, default=3); st_.add_argument("--wait-viewer", type=float, default=60.0)
    st_.add_argument("--resume", nargs="?", const="", default=None, metavar="TASK_ID",
                     help="continue a paused/waiting task (default: the latest) after a new confirmation")
    st_.add_argument("--restricted", action="store_true", help="optional safety profile: Claude has no tools, only --allow files are written, static checks only")
    sv = sub.add_parser("serve", help="open the browser viewer for a running or saved task (live updates / replay)")
    sv.add_argument("--task-id"); sv.add_argument("--timeout", type=float, default=0.0, help="stop after N seconds (default: until Ctrl+C)")
    cv = sub.add_parser("conversation", help="show the saved agent-to-agent conversation")
    cv.add_argument("--task-id"); cv.add_argument("--cycle", type=int); cv.add_argument("--message", type=int)
    cv.add_argument("--json", action="store_true")
    rp = sub.add_parser("replay", help="replay a saved conversation with its original ordering")
    rp.add_argument("--task-id"); rp.add_argument("--cycle", type=int); rp.add_argument("--delay", type=float, default=0.5)
    wt = sub.add_parser("watch", help="follow a running task's conversation from a second terminal")
    wt.add_argument("--task-id"); wt.add_argument("--poll", type=float, default=0.5)
    ap.add_argument("--width", type=int); ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--show-prompts", action="store_true", help="also print the full prompt delivered to the agent")
    ap.add_argument("--web", action="store_true", help="start the localhost browser viewer (default on for interactive run/resume/demo)")
    ap.add_argument("--no-web", action="store_true", help="do not start the browser viewer")
    ap.add_argument("--no-open", action="store_true", help="start the viewer and print its URL, but do not open a browser")
    ap.add_argument("--hold-viewer", type=float, default=0.0, help="keep the viewer running this many seconds after the task ends")
    ap.add_argument("--port", type=int, default=0, help="viewer port (default: a free port; always 127.0.0.1)")
    a = ap.parse_args(argv)
    from orchestrator import EXIT, Orchestrator
    if out is print and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace", line_buffering=True)  # never crash on an unprintable character
    color = bool(sys.stdout.isatty()) and not a.no_color and not os.environ.get("NO_COLOR") and out is print
    viewer = None if getattr(a, "quiet", False) else Viewer(out=out, width=a.width, color=color,
                                                           show_prompts=a.show_prompts)
    try:
        cfg = load_config(a.config if Path(a.config).exists() else None,
                          {"max_cycles": a.max_cycles} if getattr(a, "max_cycles", None) else None)
        runs = resolve_runs_dir(cfg)
        if a.cmd == "app":
            from launcher import run_app
            return run_app(a, out=out)
        if a.cmd == "start":
            from launcher import run_start
            a.allow = [x for item in a.allow for x in item.split(",")]
            if a.task_file:
                a.task = Path(a.task_file).read_text(encoding="utf-8-sig")
            try:
                return run_start(a, out=out, inp=inp, tty=tty, codex_runner=codex_runner, codex_base_dir=codex_base_dir,
                                 claude_runner=claude_runner, claude_which=claude_which)
            except (KeyboardInterrupt, EOFError):
                out("בוטל.")
                return EXIT["STOPPED"]
        if a.cmd == "preflight":
            _, removed = scrub_env()
            out(f"env var NAMES that will be removed from every child process: {removed}")
            out("your real environment is never modified; values are never printed or logged")
            out("live execution: Claude not implemented; Codex only via --agents hybrid behind live.enabled + approval file")
            out("codex on PATH: " + str(shutil.which("codex")) + " | claude on PATH: " + str(shutil.which("claude"))
                + " (located only, never executed)")
            return 0
        if a.cmd == "codex-preflight":  # runs the local Codex binary for version/status/features only
            from codex_adapter import CodexCli
            try:
                rep_ = CodexCli(cfg, runner=codex_runner, **({"base_dir": codex_base_dir} if codex_base_dir else {})).preflight()
            except AdapterError as e:
                out(f"BLOCKED: {e.message}")
                return 1
            out("PASS (no inference was made): " + json.dumps(rep_))
            return 0
        if a.cmd == "claude-preflight":  # runs the local Claude binary for version/auth/help only (no inference)
            from claude_adapter import ClaudeCli
            try:
                rep_ = ClaudeCli(cfg, runner=claude_runner, **({"which": claude_which} if claude_which else {})).preflight(str(HERE))
            except AdapterError as e:
                out(f"BLOCKED: {e.message}")
                return 1
            unenf = rep_.pop("unenforceable")
            out("PASS (no inference was made): " + json.dumps(rep_))
            out("NOT enforceable by this configuration (human decision needed before live use):")
            for line in unenf:
                out("  - " + line)
            return 0
        web_on = a.cmd in ("run", "resume", "demo") and (a.web or (cfg["viewer"]["web"] and out is print and not a.no_web))
        web_on = web_on and not a.no_web
        holder = {"srv": None}

        def start_web(task_id):
            holder["srv"] = web_viewer.start_for_task(runs, task_id, a.port or cfg["viewer"]["port"],
                                                      cfg["viewer"]["auto_open_browser"] and not a.no_open, out)

        def stop_web():
            srv = holder["srv"]
            if srv:
                if a.hold_viewer > 0:
                    out(f"viewer stays open for {a.hold_viewer:g}s (Ctrl+C to exit): {srv.url()}")
                    try:
                        time.sleep(a.hold_viewer)
                    except KeyboardInterrupt:
                        pass
                srv.stop()
        if a.cmd == "serve":
            tid = a.task_id or web_viewer.latest_task_id(runs)
            srv = web_viewer.start_for_task(runs, tid, a.port or cfg["viewer"]["port"], not a.no_open, out)
            try:
                end = time.time() + a.timeout if a.timeout else None
                while end is None or time.time() < end:
                    time.sleep(0.25)
            except KeyboardInterrupt:
                pass
            finally:
                srv.stop()
            return 0
        if a.cmd == "run":
            if a.dry_run:
                return dry_run(cfg, a.project, a.task, a.agents, out)
            spec = None
            if a.allow_create or a.allow_overwrite:
                from proposals import make_file_spec
                from safety import Policy
                try:
                    spec = make_file_spec(a.allow_create, a.allow_overwrite, a.require_def, Policy(a.project, cfg))
                except ValueError as e:
                    raise OrchestratorError(f"invalid file allowlist: {e}") from None
            orc = Orchestrator(cfg, a.project, runs, *build_agents(a, cfg, adapters, codex_runner, codex_base_dir, claude_runner, claude_which),
                               file_spec=spec, local_execution=a.accept_local_execution,
                               out=out, viewer=viewer)
            try:
                if web_on:
                    start_web(orc.task_id)
                st = orc.start(a.task)
            finally:
                stop_web()
            return EXIT[st["status"]]
        if a.cmd == "demo":
            import mock_scenarios
            try:
                if a.claude_sim:
                    return mock_scenarios.run_demo_claude(cfg, out, viewer, a.delay, start_web if web_on else None)
                return mock_scenarios.run_demo(cfg, out, viewer, a.delay, start_web if web_on else None, he_summaries=a.he_summaries,
                                               proposals=a.proposals)
            finally:
                stop_web()
        tid = resolve_task(runs, a.task_id)
        run_dir = runs / tid
        tpath = run_dir / "transcript.jsonl"
        if a.cmd == "status":
            s = read_json(run_dir / "state.json")
            msgs, _, _ = read_transcript(tpath)
            out(json.dumps(s, indent=2) if a.json else format_status(s)
                + ("" if a.json else f"\n  conversation: {len(msgs)} messages (last #{msgs[-1]['seq']})" if msgs else ""))
            return 0
        if a.cmd == "conversation":
            msgs, _, skipped = read_transcript(tpath)
            chosen = select(msgs, a.cycle, a.message)
            if not chosen:
                print("no matching messages", file=sys.stderr)
                return 1
            for m in chosen:
                if a.json:
                    out(json.dumps(m, ensure_ascii=False))
                else:
                    viewer.message(m)
            if skipped and not a.json:
                out(f"(note: {skipped} unreadable transcript line(s) skipped)")
            return 0
        if a.cmd == "replay":
            out(f"--- replaying task {tid} ({'cycle ' + str(a.cycle) if a.cycle else 'all cycles'}) ---")
            n = replay(tpath, viewer, a.delay, cycle=a.cycle)
            out(f"--- end of replay: {n} message(s) ---")
            return 0 if n else 1
        if a.cmd == "watch":
            out(f"--- watching task {tid}; Ctrl+C to stop ---")
            try:
                watch(tpath, viewer, lambda: read_json(run_dir / "state.json").get("status") == "RUNNING", a.poll)
            except KeyboardInterrupt:
                pass
            return 0
        s = read_json(run_dir / "state.json")
        if a.cmd in ("pause", "stop"):
            if a.cmd == "stop" and s["status"] in ("PAUSED", "HUMAN_REQUIRED"):
                s["status"], s["stop_reason"] = "STOPPED", "user_stop"
                atomic_write_json(run_dir / "state.json", s)
                EventLog(run_dir / "events.jsonl", Redactor()).write("task_end", s["cycle"], status="STOPPED")
                tr = Transcript(tpath, Redactor())
                tr.add("user", "orchestrator", "control", "stop requested", s["cycle"])
                tr.add("orchestrator", "user", "final", "STOPPED: user_stop", s["cycle"])
            else:
                write_control(run_dir, a.cmd)
            out(f"{a.cmd} requested for task {tid}")
            return 0
        if a.cmd == "extend-budget":
            from taskctl import extend_task_budget
            add = {k: v for k, v in (("checkpoint_cycles", a.cycles), ("max_minutes", a.minutes),
                                     ("max_codex_calls", a.codex_calls), ("max_claude_calls", a.claude_calls)) if v}
            s = extend_task_budget(runs, tid, add)
            out(f"budget is now {s['budget']} - run: orchestrator.py resume")
            return 0
        if a.cmd == "allow-tool":
            from taskctl import resolve_tool_permissions
            s = resolve_tool_permissions(runs, tid, a.rule, not a.deny)
            out(("approved for this task: " + ", ".join(s.get("approved_tools") or []) if not a.deny else "denied")
                + " - now run: orchestrator.py resume" + (" --agents live" if s.get("local_execution") else ""))
            return 0
        if a.cmd == "approve":
            s = resolve_approval(runs, tid, a.approval_id, not a.deny)
            out(f"approval {a.approval_id} {'denied; task failed' if a.deny else 'granted; now run resume'}")
            return 0
        if a.cmd == "resume":
            orc = Orchestrator(cfg, s["workspace"], runs, *build_agents(a, cfg, adapters, codex_runner, codex_base_dir, claude_runner, claude_which),
                               out=out, task_id=tid, viewer=viewer)
            try:
                if web_on:
                    start_web(tid)
                st = orc.resume()
            finally:
                stop_web()
            return EXIT[st["status"]]
    except (ConfigError, OrchestratorError, LiveNotEnabled, WorkspaceLocked, ActiveTaskExists, OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 2
