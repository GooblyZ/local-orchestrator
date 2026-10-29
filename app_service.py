"""Browser app actions (prepare -> confirm -> start, permissions, pause/stop/resume, files) on top of the EXISTING engine.

Nothing here talks to a model directly: tasks run through the same Orchestrator, Codex/Claude adapters, permission
store (taskctl.resolve_tool_permissions) and control file as the command line. The HTTP layer (web_viewer) authenticates
every state-changing request; this module re-validates every value it receives.

Rules kept from the terminal launcher:
- Preparing a task runs only the non-inference preflight. A model is called only after the owner confirms the prepared
  task in a SEPARATE request carrying a one-time confirmation id plus `accept_local_execution: true`. Task text is never
  read as an approval.
- One running task at a time; the same confirmation can never start two runs.
- Approvals are limited to the exact rules Claude asked for, for that task only; blanket shell access is refused by
  claude_adapter.validate_tool_rule.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path

import autonomy
import handoff
from config import HERE, OrchestratorError, load_config, resolve_runs_dir
from run_policy import budget_limits
from store import ACTIVE, atomic_write_json, read_json, write_control

PREPARE_TTL_SEC = 600
MAX_TASK_CHARS = 200_000  # task text limit (Unicode characters); the HTTP body limit is web_viewer.MAX_POST_BYTES
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


from cli_agents import LATEST_CLAUDE, default_agents, default_catalog  # noqa: E402,F401 (re-exported)


class AppService:
    def __init__(self, config_path=None, agent_factory=default_agents, workspaces_dir=None, open_folder=None,
                 catalog=default_catalog):
        self.config_path = config_path if config_path and Path(config_path).exists() else None
        self.agent_factory = agent_factory
        self.workspaces_dir = Path(workspaces_dir) if workspaces_dir else HERE / "workspaces"
        self.open_folder_fn = open_folder or (lambda p: os.startfile(p))  # noqa: S606 - local Explorer window
        self.prepared: dict[str, dict] = {}
        self.continue_lock = threading.Lock()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.active_task: str | None = None
        self.catalog_fn, self._catalog = catalog, None

    # ------------------------------------------------------------------ helpers
    def cfg(self, max_cycles: int = 3, settings: dict | None = None) -> dict:
        cfg = load_config(self.config_path, {"max_cycles": max_cycles})
        cfg["max_codex_calls"], cfg["max_claude_calls"] = 1 + max_cycles, max_cycles
        if settings:  # already validated against the installed CLIs; load_config validates the result once more
            cfg["codex"] = dict(cfg["codex"], model=settings["codex_model"], reasoning_effort=settings["codex_effort"])
            cfg["claude"] = dict(cfg["claude"], model=settings["claude_model"] or None, effort=settings["claude_effort"] or None)
            cfg = load_config(None, cfg)
        return cfg

    def prefs_path(self) -> Path:
        return self.runs() / "_app_prefs.json"

    def catalog(self) -> dict:
        if self._catalog is None:
            self._catalog = self.catalog_fn(self.cfg())
        return self._catalog

    def defaults(self) -> dict:
        cfg = self.cfg()
        d = {"codex_model": cfg["codex"]["model"], "codex_effort": cfg["codex"]["reasoning_effort"],
             "claude_model": cfg["claude"]["model"] or "", "claude_effort": cfg["claude"]["effort"] or "", "max_cycles": 3,
             "mode": "goal", "kind": "code", "autonomy": autonomy.DEFAULT_LEVEL, "fallback_codex": [], "fallback_claude": [], "script_ai_runs": 0, "ai_scripts": []}
        from goal_mode import DEFAULT_BUDGET
        d.update(DEFAULT_BUDGET)
        try:
            saved = read_json(self.prefs_path())
            d.update(self.validate_settings(saved))  # an outdated saved preference silently falls back to the defaults
        except (OSError, ValueError, AppError, TypeError, AttributeError):
            pass
        return d

    def options(self) -> dict:
        from agents import AdapterError
        try:
            cat = self.catalog()
        except AdapterError as e:
            raise AppError(503, f"BLOCKED: {e.message}")
        return {"catalog": cat, "defaults": self.defaults()}

    def validate_settings(self, s) -> dict:
        """Only values the installed CLIs actually offer; anything else is refused (no fallback, no paid tier)."""
        if not isinstance(s, dict):
            raise AppError(400, "settings must be an object")
        cat = self.catalog()
        model = next((m for m in cat["codex"] if m["slug"] == s.get("codex_model")), None)
        if not model:
            raise AppError(400, "codex_model is not offered by the installed Codex CLI")
        if s.get("codex_effort") not in model["efforts"]:
            raise AppError(400, f"codex_effort must be one of {model['efforts']} for {model['slug']}")
        cm, ce = s.get("claude_model") or "", s.get("claude_effort") or ""
        if cm and cm not in {m["id"] for m in cat["claude"]["models"]}:
            raise AppError(400, "claude_model is not offered by the installed Claude Code CLI")
        if ce and ce not in cat["claude"]["efforts"]:
            raise AppError(400, "claude_effort is not offered by the installed Claude Code CLI")
        mc = s.get("max_cycles", 3)
        if not isinstance(mc, int) or isinstance(mc, bool) or not 1 <= mc <= 5:
            raise AppError(400, "max_cycles must be 1-5")
        from goal_mode import DEFAULT_BUDGET, make_goal
        try:
            goal = make_goal(s.get("mode", "goal"), s.get("kind", "code"), {k: s[k] for k in DEFAULT_BUDGET if k in s})
            auto = autonomy.from_settings(s, {"codex": [m["slug"] for m in cat["codex"]], "claude": [m["id"] for m in cat["claude"]["models"]]})
        except (ValueError, TypeError) as e:
            raise AppError(400, str(e))
        return {**auto, "codex_model": model["slug"], "codex_effort": s["codex_effort"], "claude_model": cm, "claude_effort": ce, "max_cycles": mc,
                "mode": goal["mode"], "kind": goal["kind"], **(goal["budget"] or {k: s.get(k, v) for k, v in DEFAULT_BUDGET.items()})}

    def runs(self) -> Path:
        return resolve_runs_dir(self.cfg())

    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _state(self, task_id: str) -> tuple[Path, dict]:
        from web_viewer import TASK_RE
        if not isinstance(task_id, str) or not TASK_RE.match(task_id):
            raise AppError(400, "invalid task id")
        run_dir = self.runs() / task_id
        if not (run_dir / "state.json").exists():
            raise AppError(404, "no such task")
        return run_dir, read_json(run_dir / "state.json")

    @staticmethod
    def _refuse_mock(s: dict):
        from web_viewer import RunReader
        if RunReader.is_mock(s):
            raise AppError(409, "this is a MOCK demonstration run; it cannot be continued with real agents")

    def _run_in_background(self, task_id: str, fn):
        def target():
            try:
                fn()
            except Exception as e:  # shown to the owner; the run's own state/transcript already records agent errors
                self.last_error = f"{type(e).__name__}: {e}"
        with self.lock:
            if self.busy():
                raise AppError(409, "another task is already running")
            self.last_error, self.active_task = None, task_id
            self.thread = threading.Thread(target=target, name=f"task-{task_id}", daemon=True)
            self.thread.start()

    # ------------------------------------------------------------------ actions
    def state(self) -> dict:
        return {"running": self.busy(), "active_task": self.active_task, "last_error": self.last_error}

    def prepare(self, body: dict) -> dict:
        task = body.get("task")
        if not isinstance(task, str) or not task.strip():
            raise AppError(400, "empty_task")
        if len(task) > MAX_TASK_CHARS:
            raise AppError(400, f"task_too_long: {len(task):,} characters; the limit is {MAX_TASK_CHARS:,}")
        from agents import AdapterError
        try:
            settings = self.validate_settings(dict(self.defaults(), **(body.get("settings") or {})))
        except AdapterError as e:
            raise AppError(503, f"BLOCKED: {e.message}")
        max_cycles = settings["max_cycles"]
        folder = body.get("folder")
        cfg = self.cfg(max_cycles, settings)
        # an explicit choice; a request without one keeps the old meaning (a folder given = that folder). An existing-project
        # request without a usable folder is REFUSED - it never falls back to a new or an old workspace.
        mode = body.get("project_mode") or (handoff.EXISTING if folder else handoff.NEW)
        if mode not in (handoff.EXISTING, handoff.NEW):
            raise AppError(400, "project_mode must be existing_project or new_workspace")
        if mode == handoff.NEW and (folder or body.get("handoff_from")):
            raise AppError(400, "folder_requires_existing_project_mode")
        src = None
        if mode == handoff.EXISTING:
            if body.get("folder_authorized") is not True and isinstance(folder, str) and folder.strip():
                raise AppError(400, "folder_not_authorized")
            try:
                ws, src = handoff.prepare_existing(folder, body.get("handoff_from"), self.runs(), cfg,
                                                   self.active_task if self.busy() else None)
            except OrchestratorError as e:
                raise AppError(400, str(e))
            existing, task = True, handoff.compose_task(task, src["brief"]) if src else task
            if len(task) > MAX_TASK_CHARS:
                raise AppError(400, f"task_too_long: {len(task):,} characters; the limit is {MAX_TASK_CHARS:,}")
        else:
            from launcher import _slug
            ws, existing = self.workspaces_dir / (time.strftime("%Y%m%d-%H%M%S") + "-" + _slug(task)), False
        try:  # non-inference preflight of both CLIs (versions, subscription login, flags): no model is contacted
            self.agent_factory(cfg, ws if existing else HERE)
        except AdapterError as e:
            raise AppError(503, f"BLOCKED: {e.message}")
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.prepared.items() if now - v["created"] > PREPARE_TTL_SEC]:
                self.prepared.pop(k)
            cid = secrets.token_urlsafe(18)
            self.prepared[cid] = {"task": task.strip(), "ws": str(ws), "existing": existing, "max_cycles": max_cycles,
                                  "settings": settings, "created": now,
                                  "handoff": {k: v for k, v in src.items() if k != "brief"} if src else None}
        try:
            atomic_write_json(self.prefs_path(), settings)  # remembered as the next defaults
        except OSError:
            pass
        lim = budget_limits(cfg) if settings["mode"] == "fixed" else {"codex": settings["max_codex_calls"], "claude": settings["max_claude_calls"]}
        return {"confirm_id": cid, "task": task.strip(), "workspace": str(ws), "existing_folder": existing, "profile": "native",
                "project_mode": mode, "project_root": str(ws) if existing else None,
                "handoff": {k: v for k, v in src.items() if k != "brief"} | {"brief_chars": len(src["brief"])} if src else None,
                "allowed_tools": cfg["claude"]["allowed_tools"], "denied_tools": cfg["claude"]["disallowed_tools"],
                "max_cycles": max_cycles, "max_codex_calls": lim["codex"], "max_claude_calls": lim["claude"], "settings": settings,
                "timeout_sec": {"codex": cfg["codex"]["timeout_sec"], "claude": cfg["claude"]["timeout_sec"]}}

    def start(self, body: dict) -> dict:
        from orchestrator import Orchestrator
        if body.get("accept_local_execution") is not True:
            raise AppError(400, "local_execution_not_accepted")
        with self.lock:
            p = self.prepared.pop(str(body.get("confirm_id")), None)  # single use: a second click cannot start twice
        if not p or time.time() - p["created"] > PREPARE_TTL_SEC:
            raise AppError(400, "confirmation_expired_or_unknown")
        if self.busy():
            raise AppError(409, "another task is already running")
        cfg, ws = self.cfg(p["max_cycles"], p["settings"]), Path(p["ws"])
        project = {"mode": handoff.NEW}
        if p["existing"]:  # checked again now: the folder (or the handoff source) may have changed since it was prepared
            try:
                project = handoff.project_for_start(str(ws), p["handoff"], self.runs(), cfg)
            except OrchestratorError as e:
                raise AppError(409, str(e))
        else:
            ws.mkdir(parents=True, exist_ok=False)
        agents = self.agent_factory(cfg, ws)
        from goal_mode import DEFAULT_BUDGET, make_goal
        st_ = p["settings"]
        goal = make_goal(st_["mode"], st_["kind"], {k: st_[k] for k in DEFAULT_BUDGET})
        if goal["mode"] == "goal":  # the call budgets of goal mode live in the task state (owner-extendable)
            cfg["max_codex_calls"], cfg["max_claude_calls"] = goal["budget"]["max_codex_calls"], goal["budget"]["max_claude_calls"]
        auto = autonomy.policy_from_settings(st_)
        orc = Orchestrator(cfg, ws, resolve_runs_dir(cfg), *agents, out=lambda _m: None, local_execution=True, goal=goal,
                           project=project, autonomy_policy=auto)
        orc.run_dir.mkdir(parents=True, exist_ok=True)  # the page can open the (still empty) conversation at once
        self._run_in_background(orc.task_id, lambda: orc.start(p["task"]))
        if p["handoff"]:
            try:
                handoff.mark_source(self.runs(), p["handoff"]["from_task"], orc.task_id, str(ws))
            except OSError as e:  # the new task runs regardless; the owner is told the source could not be marked
                self.last_error = f"the source task could not be marked as handed off: {type(e).__name__}: {e}"
        return {"task_id": orc.task_id, "workspace": str(ws), "project_mode": project["mode"]}

    def _check_resumable(self, s: dict):
        """Before any action that resumes a task: not handed off, and an existing project must still be a valid root."""
        try:
            handoff.check_resumable(s, self.runs(), self.cfg())
        except OrchestratorError as e:
            raise AppError(409, str(e))

    def _resume(self, task_id: str, s: dict):
        from orchestrator import Orchestrator
        from web_viewer import RunReader
        if RunReader.is_mock(s):  # a MOCK/demo run must never be continued by the REAL agents
            raise AppError(409, "this is a MOCK demonstration run; it cannot be continued with real agents")
        self._check_resumable(s)
        a = s.get("agent_settings") or {}
        settings = {"codex_model": a["codex_model"], "codex_effort": a["codex_effort"], "claude_model": a.get("claude_model") or "",
                    "claude_effort": a.get("claude_effort") or ""} if a.get("codex_model") else None
        cfg = self.cfg(min(50, max(1, int(s.get("max_cycles") or 3))), settings)  # the SAME models the task started with
        agents = self.agent_factory(cfg, Path(s["workspace"]))
        orc = Orchestrator(cfg, s["workspace"], resolve_runs_dir(cfg), *agents, out=lambda _m: None, task_id=task_id,
                           local_execution=bool(s.get("local_execution")), file_spec=s.get("file_spec"))
        self._run_in_background(task_id, orc.resume)

    def permission(self, body: dict) -> dict:
        from taskctl import resolve_tool_permissions
        task_id, decision = body.get("task_id"), body.get("decision")
        if decision not in ("approve", "deny", "builtin_review"):
            raise AppError(400, "decision must be approve, deny or builtin_review")
        run_dir, s = self._state(task_id)
        self._refuse_mock(s)
        self._check_resumable(s)
        pending = s.get("pending_permissions") or []
        if s.get("status") != "HUMAN_REQUIRED" or not pending:
            raise AppError(409, "no permission request is pending")
        if self.busy():
            raise AppError(409, "the task is still running")
        offered = [d["suggested_rule"] for d in pending if d.get("suggested_rule")]
        rules = body.get("rules") or []
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            raise AppError(400, "rules must be a list of strings")
        if decision == "approve":
            if not rules or not set(rules) <= set(offered):  # only the EXACT rules Claude asked for
                raise AppError(400, "only the exact requested rule(s) can be approved")
        if decision == "builtin_review" and not all(d.get("kind") == "script" for d in pending):
            raise AppError(400, "the built-in review replaces a pending project-script request only")
        resolve_tool_permissions(self.runs(), task_id, rules if decision == "approve" else [], decision == "approve",
                                 builtin_review=decision == "builtin_review")
        self._resume(task_id, read_json(run_dir / "state.json"))
        return {"ok": True, "resumed": True}

    def control(self, body: dict) -> dict:
        from conversation import Transcript
        from safety import Redactor
        from store import EventLog
        task_id, action = body.get("task_id"), body.get("action")
        if action not in ("pause", "stop", "resume"):
            raise AppError(400, "action must be pause, stop or resume")
        run_dir, s = self._state(task_id)
        running = self.busy() and self.active_task == task_id
        if action == "resume":
            from safety import Redactor as _R
            from web_viewer import RunReader
            interrupted = s.get("status") == "RUNNING" and RunReader(run_dir, _R()).run_state(s) == "interrupted"
            asked = (s.get("status") == "HUMAN_REQUIRED" and not s.get("pending_permissions")
                     and str(s.get("stop_reason") or "").startswith(("stalled", "reviewer_requested_human", "review_loop",
                                                                 "acceptance_unverifiable", "model_authorization_required",
                                                                 "mission_capabilities_required")))
            from orchestrator import format_recoverable
            if running or not (s.get("status") == "PAUSED" or interrupted or asked or format_recoverable(s)):
                raise AppError(409, "only a paused, interrupted or waiting task can be resumed")
            self._resume(task_id, s)
            return {"ok": True}
        if s.get("status") not in ACTIVE:
            raise AppError(409, f"task is {s.get('status')}")
        if action == "stop" and not running:  # nothing is executing: end it now (same as `orchestrator.py stop`)
            s["status"], s["stop_reason"] = "STOPPED", "user_stop"
            atomic_write_json(run_dir / "state.json", s)
            EventLog(run_dir / "events.jsonl", Redactor()).write("task_end", s["cycle"], status="STOPPED")
            tr = Transcript(run_dir / "transcript.jsonl", Redactor())
            tr.add("user", "orchestrator", "control", "stop requested", s["cycle"])
            tr.add("orchestrator", "user", "final", "STOPPED: user_stop", s["cycle"])
            return {"ok": True}
        if not running:
            raise AppError(409, "the task is not running")
        write_control(run_dir, action)  # stop also cancels a running agent call (process tree killed)
        return {"ok": True}

    def files(self, task_id: str) -> dict:
        _, s = self._state(task_id)
        ws, out = Path(s["workspace"]), []
        for dirpath, dirnames, filenames in os.walk(ws):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
            for n in sorted(filenames):
                p = Path(dirpath) / n
                if not p.is_symlink():
                    out.append({"path": p.relative_to(ws).as_posix(), "bytes": p.stat().st_size})
                if len(out) >= 200:
                    return {"workspace": str(ws), "files": out, "truncated": True}
        return {"workspace": str(ws), "files": out, "truncated": False}

    def read_file(self, task_id: str, rel: str) -> dict:
        from safety import Policy, PolicyViolation, Redactor, clip
        _, s = self._state(task_id)
        try:
            p, rel_ok = Policy(s["workspace"], self.cfg()).check_path(str(rel), "read")
        except PolicyViolation as e:
            raise AppError(403, str(e))
        if not p.is_file() or p.is_symlink():
            raise AppError(404, "no such file")
        data = p.read_bytes()[:200_000]
        if b"\x00" in data[:4096]:
            raise AppError(415, "binary file")
        return {"path": rel_ok, "content": clip(Redactor().text(data.decode("utf-8", "replace")), 200_000)}

    def budget(self, body: dict) -> dict:
        """OWNER action from the browser: extend a paused goal-mode task's budget, then resume it."""
        from taskctl import extend_task_budget
        task_id, add = body.get("task_id"), body.get("add")
        run_dir, s = self._state(task_id)
        self._refuse_mock(s)
        self._check_resumable(s)
        if self.busy() and self.active_task == task_id:
            raise AppError(409, "the task is still running")
        if not isinstance(add, dict):
            raise AppError(400, "add must be an object")
        try:
            extend_task_budget(self.runs(), task_id, add)
        except OrchestratorError as e:
            raise AppError(400, str(e))
        self._resume(task_id, read_json(run_dir / "state.json"))
        return {"ok": True}

    def continue_task(self, body: dict) -> dict:
        """OWNER action: continue a saved goal-mode task, optionally with new instructions and a budget extension. Explicit
        confirmation is required; one continuation at a time; never while the task (or any task) runs."""
        from taskctl import prepare_continuation
        from web_viewer import RunReader
        from safety import Redactor
        task_id = body.get("task_id")
        run_dir, s = self._state(task_id)
        self._refuse_mock(s)
        self._check_resumable(s)
        if body.get("confirm") is not True:
            raise AppError(400, "confirmation_required")
        note, add = body.get("note") or "", body.get("add") or None
        if not isinstance(note, str) or (add is not None and not isinstance(add, dict)):
            raise AppError(400, "note must be text and add an object")
        if not self.continue_lock.acquire(blocking=False):
            raise AppError(409, "a continuation is already being started")
        try:
            if self.busy():
                raise AppError(409, "another task is already running")
            if s.get("status") == "RUNNING" and RunReader(run_dir, Redactor()).run_state(s) != "interrupted":
                raise AppError(409, "the task is running")
            try:
                entry = prepare_continuation(self.runs(), task_id, note, add, single_pass=body.get("single_pass") is True)
            except OrchestratorError as e:
                raise AppError(409 if str(e).startswith(("budget_insufficient", "continuation_already_pending", "task is")) else 400, str(e))
            self._resume(task_id, read_json(run_dir / "state.json"))
            return {"ok": True, "continuation": entry["n"]}
        finally:
            self.continue_lock.release()

    def summarize(self, body: dict) -> dict:
        """OWNER action: one Codex call that writes a status summary of a SAVED task from its preserved files and records.
        Only with explicit confirmation, never while the task runs, and counted in the task's Codex budget."""
        import json
        from orchestrator import set_status_summary
        from safety import Redactor
        from store import EventLog
        from web_viewer import RunReader
        task_id = body.get("task_id")
        run_dir, s = self._state(task_id)
        self._refuse_mock(s)
        if body.get("confirm") is not True:
            raise AppError(400, "confirmation_required")
        if self.busy() or (s.get("status") == "RUNNING" and RunReader(run_dir, Redactor()).run_state(s) != "interrupted"):
            raise AppError(409, "the task is running; a summary is written by the agents as part of their work")
        b, calls = s.get("budget"), s.get("calls") or {}
        if b and calls.get("codex", 0) >= b["max_codex_calls"]:
            raise AppError(409, "codex_budget_exhausted: extend the budget first")

        def work():
            import research_review
            a = s.get("agent_settings") or {}
            settings = {"codex_model": a["codex_model"], "codex_effort": a["codex_effort"], "claude_model": a.get("claude_model") or "",
                        "claude_effort": a.get("claude_effort") or ""} if a.get("codex_model") else None
            cfg = self.cfg(min(50, max(1, int(s.get("max_cycles") or 3))), settings)
            reviewer = self.agent_factory(cfg, Path(s["workspace"]))[2]
            data = {"status": s.get("status"), "stop_reason": s.get("stop_reason"), "cycle": s.get("cycle"),
                    "history": (s.get("history") or [])[-6:], "criteria_status": s.get("criteria_status"),
                    "last_evidence": s.get("last_evidence"), "previous_status_summary": (s.get("status_summary") or {}).get("sections")}
            read = []
            if s.get("kind") == "research":
                fixed = len(s["task"]) + len(json.dumps(s.get("acceptance_criteria") or [], ensure_ascii=False)) + 60_000
                budget = max(10_000, min(cfg["review_material_chars"], int((cfg["codex"]["max_prompt_chars"] - fixed) / 1.15)))
                mat = research_review.prepare(s["workspace"], budget)
                part = mat["parts"][0] if mat["parts"] else []
                read = sorted({seg["path"] for seg in part})
                data["material_read"] = read
                data["material_not_read"] = [f["path"] for f in mat["manifest"]["files"] if f["path"] not in read]
                data["research_material"] = research_review.part_view(part)
            fresh = read_json(run_dir / "state.json")  # the call is counted BEFORE it is made, like every model call
            fresh.setdefault("calls", {"codex": 0, "claude": 0})["codex"] = fresh["calls"].get("codex", 0) + 1
            atomic_write_json(run_dir / "state.json", fresh)
            raw = reviewer.summarize({"task": s["task"], "acceptance_criteria": s.get("acceptance_criteria") or [], "data": data})
            from agents import parse_json_object, valid_status_summary
            sections = valid_status_summary(parse_json_object(raw).get("status_summary_he"))
            if sections is None:
                raise OrchestratorError("Codex's summary was malformed; the previous summary is kept")
            fresh = read_json(run_dir / "state.json")
            from conversation import _last_seq
            set_status_summary(fresh, sections, "codex", fresh.get("cycle"), on_request=True, basis={"read": read},
                               after_seq=_last_seq(run_dir / "transcript.jsonl"))
            atomic_write_json(run_dir / "state.json", fresh)
            EventLog(run_dir / "events.jsonl", Redactor()).write("status_summary", fresh.get("cycle"), author="codex", on_request=True)
        self._run_in_background(task_id, work)
        return {"ok": True}

    def set_autonomy(self, body: dict) -> dict:
        """OWNER action: a saved task's autonomy level, fallback models and AI-run allowance (applies from the next step)."""
        from agents import AdapterError
        from taskctl import set_task_autonomy
        task_id = body.get("task_id")
        self._refuse_mock(self._state(task_id)[1])
        if self.busy() and self.active_task == task_id:
            raise AppError(409, "the task is running; pause it first")
        try:  # every model is checked against what the INSTALLED CLIs offer (no inference, never a paid tier)
            cat = self.catalog()
            offered = {"codex": [m["slug"] for m in cat["codex"]], "claude": [m["id"] for m in cat["claude"]["models"]]}
            set_task_autonomy(self.runs(), task_id, body.get("level"), offered=offered, **{
                k: body[k] for k in ("fallback_codex", "fallback_claude", "script_ai_runs", "use_model", "agent_reviews", "ai_scripts") if k in body})
        except (OrchestratorError, ValueError, TypeError, AdapterError) as e:
            raise AppError(400, str(getattr(e, "message", e)))
        return {"ok": True}

    def open_folder(self, body: dict) -> dict:
        _, s = self._state(body.get("task_id"))
        self.open_folder_fn(s["workspace"])
        return {"ok": True}
