"""Task-level helpers: lookup, approvals, status formatting, dry run (no Orchestrator dependency)."""
from __future__ import annotations

from pathlib import Path

from config import OrchestratorError, resolve_runs_dir, validate_workspace
from safety import Policy, PolicyViolation, Redactor, scrub_env
from conversation import Transcript
from store import EventLog, atomic_write_json, read_json

def latest_task(runs_dir) -> str | None:
    states = sorted(Path(runs_dir).glob("*/state.json"), key=lambda p: p.stat().st_mtime)
    return states[-1].parent.name if states else None


def resolve_task(runs_dir, task_id):
    tid = task_id or latest_task(runs_dir)
    if not tid or not (Path(runs_dir) / tid / "state.json").exists():
        raise OrchestratorError("no task found (use --task-id)")
    return tid


def resolve_approval(runs_dir, task_id, approval_id, approve: bool):
    run_dir = Path(runs_dir) / task_id
    s = read_json(run_dir / "state.json")
    a = next((x for x in s["approvals"] if x["id"] == approval_id and x["status"] == "pending"), None)
    if not a:
        raise OrchestratorError(f"no pending approval {approval_id}")
    a["status"] = "approved" if approve else "denied"
    log = EventLog(run_dir / "events.jsonl", Redactor())
    log.write("approval_resolved", s["cycle"], approval=a)
    tr = Transcript(run_dir / "transcript.jsonl", Redactor())
    tr.add("user", "orchestrator", "approval_decision",
           f"{'APPROVED' if approve else 'DENIED'} {a['id']}: {a['op']} {a['path']}", s["cycle"])
    if not approve:
        s["status"], s["stop_reason"] = "FAILED", "approval_denied"
        tr.add("orchestrator", "user", "final", "FAILED: approval_denied", s["cycle"])
    atomic_write_json(run_dir / "state.json", Redactor().obj(s))
    return s


_SCRIPT_RULE = __import__("re").compile(r"^script:[A-Za-z0-9_./ -]{1,200}\.py@[0-9a-f]{64}$")


BUILTIN_REVIEW_NOTE = (
    "The project owner did NOT approve running {scripts}, and it must not be run. Instead, use the orchestrator's BUILT-IN "
    "Codex review (an agent operation: no script, no CLI, the task's authorized model and budget): (1) export the saved "
    "artifact(s) that review was meant to cover - e.g. the script version and its concept - as text file(s) inside the "
    "project, with a small export script that only READS the saved database (sqlite3 with mode=ro or immutable=1; only "
    "sqlite3/json/pathlib - no subprocess, network or deletions, so the gateway runs it without approval; never modify a "
    "database); (2) in your JSON report add \"agent_requests\": [{{\"op\": \"codex_review\", \"artifact\": \"<exported file>\", "
    "\"focus\": \"<what the failed review was meant to check>\", \"id\": \"<label>\"}}]. The orchestrator runs the review "
    "right after your step and gives you the result at your next step.")


def resolve_tool_permissions(runs_dir, task_id, rules, approve: bool, builtin_review: bool = False) -> dict:
    """The owner's decision on Claude's pending permission request, for THIS task only. Approved rules are added to this
    task's --allowedTools on resume; denied ones are reported to Claude so it can finish without them."""
    from claude_adapter import validate_tool_rule
    run_dir = Path(runs_dir) / task_id
    s = read_json(run_dir / "state.json")
    pending = s.get("pending_permissions") or []
    if approve:
        chosen = [r if _SCRIPT_RULE.match(str(r)) else validate_tool_rule(r)
                  for r in (rules or [d["suggested_rule"] for d in pending if d.get("suggested_rule")])]
    else:  # a refusal may name requests that could never be approved (e.g. a destructive command); nothing is granted
        chosen = list(rules or []) or [d.get("suggested_rule") or f"{d.get('tool')}({d.get('input', '')})" for d in pending]
    if not chosen:
        raise OrchestratorError("no permission request is pending (or it has no approvable rule); pass --rule")
    if s.get("status") != "HUMAN_REQUIRED":
        raise OrchestratorError(f"task is {s.get('status')}, not waiting for a permission decision")
    tr = Transcript(run_dir / "transcript.jsonl", Redactor())
    tr.add("user", "orchestrator", "approval_decision", f"{'APPROVED' if approve else 'NOT RUN - routed to the built-in Codex review' if builtin_review else 'DENIED'} for this task only: " + ", ".join(chosen), s["cycle"])
    EventLog(run_dir / "events.jsonl", Redactor()).write("permission_decision", s["cycle"], approve=approve, rules=chosen)
    if approve:
        scripts = [r for r in chosen if _SCRIPT_RULE.match(r)]
        s["approved_tools"] = sorted(set(s.get("approved_tools") or []) | (set(chosen) - set(scripts)))
        ai_found = set()
        for d in pending:  # re-screened NOW with the current analyzer (a stored finding may come from an older screening)
            rule = d.get("suggested_rule") or ""
            if not _SCRIPT_RULE.match(rule):
                continue
            try:
                import capgate
                g = capgate.Gate({"root": s["workspace"], "audit": "", "protected": []})
                found = g.screen(g.inside(rule[len("script:"):].rsplit("@", 1)[0]))
            except (ValueError, OSError):
                found = d.get("findings") or []
            if any(str(f).startswith("agent_cli") for f in found):
                ai_found.add(rule)
        for r in scripts:
            path, sha = r[len("script:"):].rsplit("@", 1)
            if r in ai_found and isinstance(s.get("autonomy"), dict):
                # a script that starts the AI CLIs is ALSO declared as an AI-integration script of this mission (metered by
                # the shim; later edits WITHOUT network/system use need no new approval). The owner's approval of THIS exact
                # version is always kept below as well - it covers everything the card showed (e.g. its network use).
                import autonomy as _a
                a = dict(s["autonomy"])
                a["ai_scripts"] = sorted(set(a.get("ai_scripts") or []) | {path})[:10]
                a["script_ai_runs"] = max(1, int(a.get("script_ai_runs") or 0))
                s["autonomy"] = _a.make_autonomy(a["level"], a.get("fallback_models"), a["script_ai_runs"], a.get("agent_reviews"),
                                                 a["ai_scripts"])
            if {"path": path, "sha256": sha} not in (s.get("approved_scripts") or []):  # one exact version, this task only
                s.setdefault("approved_scripts", []).append({"path": path, "sha256": sha})
        note = ("The project owner approved for this task: " + ", ".join(chosen) + ". Continue the previous instruction"
                + (" and run the approved script(s) now through the capability gateway (unchanged file content only)." if scripts else "."))
    elif builtin_review:  # the owner routes the pending AI review to the orchestrator's typed operation; nothing is approved
        note = BUILTIN_REVIEW_NOTE.format(scripts=", ".join(r.split("@")[0].replace("script:", "") for r in chosen))
    else:
        note = ("The project owner did NOT approve: " + ", ".join(chosen) + ". Complete the previous instruction without it, "
                "or explain in remaining_issues why that is impossible.")
    s["instruction"] = note + "\n\nPrevious instruction:\n" + (s.get("instruction") or "")
    s["instruction_source"], s["pending_permissions"] = "orchestrator", None
    atomic_write_json(run_dir / "state.json", Redactor().obj(s))
    return s


def extend_task_budget(runs_dir, task_id, add: dict) -> dict:
    """OWNER action: raise a goal-mode task's budgets (never lower, capped). Counters are kept; then resume."""
    from goal_mode import extend_budget
    run_dir = Path(runs_dir) / task_id
    s = read_json(run_dir / "state.json")
    if s.get("status") not in ("PAUSED", "HUMAN_REQUIRED"):
        raise OrchestratorError(f"task is {s.get('status')}; budgets can be extended while it is paused")
    try:
        new = extend_budget(s, add)
    except ValueError as e:
        raise OrchestratorError(str(e)) from None
    old, s["budget"] = s["budget"], new
    EventLog(run_dir / "events.jsonl", Redactor()).write("budget_extended", s["cycle"], old=old, new=new, by="owner")
    Transcript(run_dir / "transcript.jsonl", Redactor()).add(
        "user", "orchestrator", "approval_decision", "BUDGET EXTENDED by the owner: " + ", ".join(
            f"{k} {old[k]} -> {new[k]}" for k in new if new[k] != old[k]), s["cycle"])
    atomic_write_json(run_dir / "state.json", Redactor().obj(s))
    return s


CONTINUABLE = ("COMPLETE", "INCONCLUSIVE", "STOPPED", "FAILED", "BLOCKED", "NEEDS_REVISION", "PAUSED", "HUMAN_REQUIRED",
               "RUNNING")  # RUNNING only when its process is gone (interrupted); the app checks that before calling this
MAX_NOTE_CHARS = 20_000
MIN_CONTINUE = {"minutes": 1, "codex": 2, "claude": 1}  # a continuation needs Codex's plan, one Claude step and one review


def remaining_budget(s: dict) -> dict:
    b, calls = s.get("budget") or {}, s.get("calls") or {}
    return {"minutes": round(b.get("max_minutes", 0) - (s.get("elapsed_sec") or 0) / 60, 1),
            "codex": b.get("max_codex_calls", 0) - calls.get("codex", 0), "claude": b.get("max_claude_calls", 0) - calls.get("claude", 0)}


def _report_verdict(s: dict):
    """The current decision line of a research report (read-only), e.g. INCONCLUSIVE; None for code tasks or no report."""
    if s.get("kind") != "research":
        return None
    try:
        from goal_mode import research_evidence
        return research_evidence(s["workspace"], s["task"]).get("verdict")
    except Exception:
        return None


def prepare_continuation(runs_dir, task_id, note: str = "", add: dict | None = None, single_pass: bool = False) -> dict:
    """OWNER action: reopen a saved goal-mode task for more work. Nothing is reset or rewritten: the original task, history,
    files, verdicts, counters, permissions and models stay; the owner's note is recorded as a NEW, separately labelled
    instruction; an optional budget extension is applied only upwards (goal_mode.extend_budget). No model is called here:
    the continuation starts when the task is resumed (Codex first plans it from the record and the note)."""
    from goal_mode import extend_budget
    from store import now
    run_dir = Path(runs_dir) / task_id
    s = read_json(run_dir / "state.json")
    if s.get("status") not in CONTINUABLE:
        raise OrchestratorError(f"task is {s.get('status')} and cannot be continued")
    if not s.get("budget") or s.get("mode") != "goal":
        raise OrchestratorError("not_goal_mode: only tasks created in goal mode (with a time and call budget) can be continued")
    if s.get("continuation_pending") is not None:
        raise OrchestratorError("continuation_already_pending: a continuation is already authorized; resume the task")
    note = str(note or "").strip()
    if len(note) > MAX_NOTE_CHARS:
        raise OrchestratorError(f"note_too_long: {len(note):,} characters; the limit is {MAX_NOTE_CHARS:,}")
    budget_before = dict(s["budget"])
    if add:
        try:
            s["budget"] = extend_budget(s, add)
        except ValueError as e:
            raise OrchestratorError(str(e)) from None
    left = remaining_budget(s)
    short = [k for k, v in MIN_CONTINUE.items() if left[k] < v]
    if short:
        raise OrchestratorError("budget_insufficient: " + ", ".join(f"{k} left {left[k]}" for k in short)
                                + "; extend the budget to continue")
    red = Redactor()
    prior = {"status": s.get("status"), "stop_reason": s.get("stop_reason"), "cycle": s.get("cycle"),
             "verdict": _report_verdict(s),
             "bottom_line": ((s.get("status_summary") or {}).get("sections") or {}).get("bottom_line")}
    entry = {"n": len(s.get("continuations") or []) + 1, "at": now(), "note": red.text(note), "prior": prior, "single_pass": bool(single_pass),
             "budget_before": budget_before, "budget_after": s["budget"], "extension": add or None,
             "consumed": {"minutes": round((s.get("elapsed_sec") or 0) / 60, 1), **(s.get("calls") or {})}, "remaining": left}
    from store import clear_control
    clear_control(run_dir)  # a leftover 'stop' from how the task ended must not cancel the continuation the owner just authorized
    s.setdefault("continuations", []).append(entry)
    s["continuation_pending"] = entry["n"]
    s["status"], s["stop_reason"], s["pending_question"] = "PAUSED", f"owner_continuation: #{entry['n']}", None
    s.pop("review_pending", None)
    log = EventLog(run_dir / "events.jsonl", red)
    log.write("continuation_authorized", s["cycle"], n=entry["n"], with_note=bool(note), extension=add or None, remaining=left)
    if add:
        log.write("budget_extended", s["cycle"], old=budget_before, new=s["budget"], by="owner")
    Transcript(run_dir / "transcript.jsonl", red).add("user", "codex", "continuation", entry, s["cycle"])
    atomic_write_json(run_dir / "state.json", red.obj(s))
    return entry


def set_task_autonomy(runs_dir, task_id, level, offered=None, fallback_codex=None, fallback_claude=None, script_ai_runs=None,
                      use_model=None, agent_reviews=None, ai_scripts=None) -> dict:
    """OWNER action (browser or command line only) on a saved task: autonomy level, fallback models, the allowance of AI CLI
    runs by project scripts, and (answering a model-authorization request) the model to use from now on. Anything not given
    is kept. Models must be offered by the installed CLIs. History, budgets, approvals and files are untouched; logged."""
    import autonomy
    import handoff
    run_dir = Path(runs_dir) / task_id
    s = read_json(run_dir / "state.json")
    if handoff.handed_off(s):
        raise OrchestratorError("handed_off: this task is kept for reference; change the task that continues its work")
    prev = s.get("autonomy") or {}
    fb = dict((prev.get("fallback_models") or {"codex": [], "claude": []}))
    for agent, models in (("codex", fallback_codex), ("claude", fallback_claude)):
        if models is not None:
            if not isinstance(models, list) or (offered is not None and not set(models) <= set(offered.get(agent) or [])):
                raise ValueError(f"fallback_{agent} must list models the installed {agent} CLI offers")
            fb[agent] = list(models)
    runs = prev.get("script_ai_runs", 0) if script_ai_runs is None else script_ai_runs
    lvl = level or prev.get("level") or autonomy.DEFAULT_LEVEL
    reviews = prev.get("agent_reviews") if agent_reviews is None else agent_reviews
    scripts = prev.get("ai_scripts") or [] if ai_scripts is None else ai_scripts
    new = autonomy.make_autonomy(lvl, fb, runs, reviews if lvl == prev.get("level") or agent_reviews is not None else None, scripts)
    changes = [f"level {prev.get('level', 'legacy')} -> {new['level']}", f"fallbacks {fb}", f"AI CLI calls by declared scripts {runs}",
               f"declared AI-integration scripts {new['ai_scripts']}", f"Codex artifact reviews {new['agent_reviews']}"]
    if use_model is not None:  # the owner picks the model a task uses from now on (e.g. after 'model unavailable')
        agent, model = (use_model or {}).get("agent"), (use_model or {}).get("model")
        if agent not in ("codex", "claude") or not isinstance(model, str) or (offered is not None and model not in (offered.get(agent) or [])):
            raise ValueError("use_model must name a model the installed CLI offers")
        key = "codex_model" if agent == "codex" else "claude_model"
        changes.append(f"{agent} model {(s.get('agent_settings') or {}).get(key)} -> {model}")
        s.setdefault("agent_settings", {})[key] = model
        if model not in new["fallback_models"][agent]:
            new["fallback_models"][agent] = (new["fallback_models"][agent] + [model])[-3:]
    s["autonomy"] = new
    EventLog(run_dir / "events.jsonl", Redactor()).write("autonomy_changed", s.get("cycle"), changes=changes, by="owner")
    Transcript(run_dir / "transcript.jsonl", Redactor()).add("user", "orchestrator", "approval_decision",
                                                             "AUTONOMY set by the owner: " + "; ".join(changes), s.get("cycle"))
    atomic_write_json(run_dir / "state.json", Redactor().obj(s))
    return s


def format_status(s) -> str:
    lines = [f"task {s['task_id']}: {s['status']}" + (f" ({s['stop_reason']})" if s["stop_reason"] else ""),
             f"  cycle {s['cycle']}/{s['max_cycles']}   no-progress streak: {s['no_progress_streak']}",
             f"  workspace: {s['workspace']}"]
    if s.get("pending_question"):
        lines.append(f"  waiting for you: {s['pending_question']}")
    for h in s["history"][-5:]:
        lines.append(f"  cycle {h['cycle']}: exec={h['executor']} verify={h['verification']} "
                     f"review={h['reviewer']} -> {h['decision']} progress={h['progress']}")
    return "\n".join(lines)


def dry_run(cfg, workspace, task, agents, out=print) -> int:
    """No state, no logs, no agent calls, no subprocesses."""
    runs = resolve_runs_dir(cfg)
    ws = validate_workspace(workspace, runs)
    _, removed = scrub_env()
    pol = Policy(ws, cfg)
    try:
        pol.check_command(cfg["verify_command"])
        vc = "allowed by policy"
    except PolicyViolation as e:
        vc = f"BLOCKED ({e})"
    for line in ("DRY RUN - nothing will be executed or written", f"task: {task}", f"workspace: {ws}",
                 f"agents: {agents} (live Claude not implemented; Codex only via hybrid mode behind config flag + approval file)",
                 f"limits: cycles={cfg['max_cycles']} no-progress={cfg['max_no_progress']} "
                 f"agent-timeout={cfg['process_timeout_sec']}s verify-timeout={cfg['verification_timeout_sec']}s",
                 f"verification command: {cfg['verify_command']} -> {vc}",
                 f"env var NAMES removed from child processes: {removed}",
                 "loop: plan -> [execute -> verify -> review]* until COMPLETE | HUMAN_REQUIRED | FAILED | limit"):
        out(line)
    return 0
