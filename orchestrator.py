#!/usr/bin/env python3
"""Local AI orchestrator (Phase 2: MOCK agents only).

User -> Codex planner -> Claude executor -> deterministic verification -> Codex reviewer -> repeat/stop.
No real Codex/Claude process is ever started in this phase. See README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path

from conversation import Transcript
from agents import (AdapterError, LiveNotEnabled, make_mock_agents,
                    parse_json_object, validate_plan, validate_report, validate_review)
from safety import (ApprovalRequired, GuardedFS, Policy, PolicyViolation, ProcessCancelled,
                    ProcessTimeout, Redactor, clip, diff_snapshots, git_status, run_process,
                    scrub_env, snapshot, snapshot_digest, tests_ran)
from store import (ACTIVE, ActiveTaskExists, EventLog, WorkspaceLocked, acquire_lock,
                   atomic_write_json, clear_control, find_active_tasks, now, read_control,
                   read_json, release_lock, write_control)

from proposals import (NoToolsFS, ProposalApplier, apply_step, current_files, not_run_evidence, static_evidence,
                       validate_proposal_report)
from goal_mode import budget_stop, checkpoint_due, checkpoint_summary, research_evidence, stall_reason
import autonomy
import handoff
import research_review
from autopilot import AutopilotMixin
from claude_adapter import CAPGATE_CMD
from run_policy import (DYNAMIC_EXECUTION_BLOCKED, budget_limits, changed_file_contents, compact_evidence,
                        executor_is_trusted, status_for_error)
from config import (DEFAULT_CONFIG, HERE, ConfigError, OrchestratorError, load_config,
                    resolve_runs_dir, validate_workspace)

REPORT_CORRECTIONS = 2  # same-session requests for a corrected report after a FORMAT rejection (bounded; each a counted call)
PLAN_CORRECTIONS = 2  # correction requests after a planner answer that fails format validation (bounded; each is a counted call)
def set_status_summary(s: dict, sections: dict, author: str, cycle, final=False, **extra) -> None:
    prev = s.get("status_summary")
    if prev:
        hist = (s.get("status_summary_history") or []) + [{k: prev.get(k) for k in ("author", "cycle", "at", "final")}
                                                          | {"bottom_line": prev["sections"].get("bottom_line", "")}]
        s["status_summary_history"] = hist[-20:]
    s["status_summary"] = {"author": author, "cycle": cycle, "at": now(), "final": bool(final),
                           "reviewed_by_codex": author == "codex", "sections": sections, **extra}


def format_recoverable(s: dict) -> bool:
    """A research run that FAILED only because a report's format was rejected, or because the independent review failed
    TECHNICALLY: its work is intact and Codex can review it (review-only; Claude is not re-run)."""
    return (s.get("status") == "FAILED" and s.get("kind") == "research" and bool(s.get("plan"))
            and str(s.get("stop_reason") or "").startswith(("malformed_output", "reviewer_error")))


LONG_TASK_CHARS = 4000  # above this, the full task is also saved for Claude in .orchestrator/TASK.md
TASK_DIR = ".orchestrator"

FATAL = {"auth": "auth_error", "quota": "quota_exceeded", "malformed": "malformed_output",
         "cancelled": "cancelled", "config": "configuration_error"}
EXIT = {"COMPLETE": 0, "FAILED": 1, "HUMAN_REQUIRED": 3, "PAUSED": 4, "STOPPED": 5, "NEEDS_REVISION": 6, "BLOCKED": 7,
        "INCONCLUSIVE": 8}


class Orchestrator(AutopilotMixin):
    def __init__(self, cfg, workspace, runs_dir, planner, executor, reviewer, *, out=print,
                 after_cycle=None, redactor=None, task_id=None, viewer=None, file_spec=None, local_execution=False,
                 goal=None, project=None, autonomy_policy=None):
        self.cfg, self.runs_dir = cfg, Path(runs_dir)
        self.workspace = validate_workspace(workspace, runs_dir)
        self.autonomy = autonomy_policy  # the owner's autonomy choice (autonomy.make_autonomy); None = supervised
        self.project = project  # None (command line / tests), {"mode": "new_workspace"} or an authorized existing project
        if handoff.is_existing({"project": project}) and os.path.normcase(os.path.realpath(project["root"])) != os.path.normcase(str(self.workspace)):
            raise OrchestratorError("the workspace is not the authorized project root")
        self.planner, self.executor, self.reviewer = planner, executor, reviewer
        self.out, self.after_cycle, self.viewer = out, after_cycle, viewer
        self.transcript: Transcript | None = None
        self.redactor = redactor or Redactor()
        self.task_id = task_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.run_dir = self.runs_dir / self.task_id
        self.state_path = self.run_dir / "state.json"
        self.policy = Policy(self.workspace, cfg)
        for agent in (planner, executor, reviewer):  # real CLI adapters are monitored mid-call (stop, budget, quiet)
            if hasattr(agent, "set_cancel_check"):
                agent.set_cancel_check(self._call_monitor)
        for agent in (planner, executor, reviewer):  # streamed CLI events -> live activity lines
            if hasattr(agent, "set_activity_sink"):
                agent.set_activity_sink(self._activity)
        self.child_env, self.removed_env = scrub_env()  # copy; the real environment is untouched
        self.state: dict = {}
        self.log: EventLog | None = None
        self.baseline: dict = {}
        self._lock = None
        self.file_spec = file_spec  # RESTRICTED profile: the owner's allowlist (proposals.make_file_spec)
        self.local_execution = local_execution  # NATIVE profile: the owner accepted local execution for this task
        self.goal = goal or {"mode": "fixed", "kind": "code", "budget": None}  # goal_mode.make_goal
        self._t0, self._elapsed_base = None, 0.0
        self._interrupt = None  # why a running model call was stopped: None (owner stop) or a budget reason
        self._last_activity, self._quiet_notices = time.time(), 0

    # ------------------------------------------------------------------ plumbing
    def _msg(self, sender, recipient, kind, content, cycle=None, **meta):
        if self.transcript:
            return self.transcript.add(sender, recipient, kind, content, cycle=cycle, **meta)

    def _activity(self, rec):
        if not rec.get("notice"):  # an agent did something observable
            self._last_activity, self._quiet_notices = time.time(), 0
        try:  # display-only: a log or viewer problem must never break (or orphan) a running agent call
            self.log.write("activity", self.state.get("cycle"), **rec)
            if self.viewer:
                self.viewer.activity(rec)
        except Exception:
            pass

    def _internal_error(self, e: BaseException):
        """An unexpected exception ended the run: record the ACTUAL error and leave the task PAUSED (resumable, budgets
        kept) instead of a stale RUNNING. If state.json itself cannot be written, the error goes to internal_error.json."""
        s = self.state
        if not s:
            return
        msg = self.redactor.text(f"internal_error: {type(e).__name__}: {e}")[:600]
        rec = {"error": msg, "at": now(), "cycle": s.get("cycle"),
               "traceback": self.redactor.text(traceback.format_exc())[-3000:]}
        s["status"], s["stop_reason"] = "PAUSED", msg
        try:
            self.log.write("internal_error", s.get("cycle"), error=msg, traceback=rec["traceback"])
        except Exception:
            pass
        try:
            self._save()
            return
        except Exception as e2:
            rec["state_save_error"] = self.redactor.text(f"{type(e2).__name__}: {e2}")[:600]
        try:  # a separate file that the viewer does not poll, so this write cannot be blocked the same way
            (self.run_dir / "internal_error.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def _say(self, msg):
        self.out(self.redactor.text(msg))

    def _save(self):
        self.state["updated_at"] = now()
        if self._t0 is not None:  # running time only (paused / waiting time does not count against the budget)
            self.state["elapsed_sec"] = round(self._elapsed_base + time.time() - self._t0, 1)
        atomic_write_json(self.state_path, self.redactor.obj(self.state))

    def _snap(self):
        return snapshot(self.workspace, list(self.cfg["snapshot_ignore_dirs"]) + [TASK_DIR])

    def _task_file(self):
        """A long task is kept in full in <workspace>/.orchestrator/TASK.md so the native agent can re-read every detail
        without the whole text being re-sent each cycle. Checked by hash before each cycle and restored if changed."""
        s = self.state
        tf = s.get("task_file")
        if not tf:
            return None
        p = self.workspace / tf["path"]
        data = s["task"].encode("utf-8")
        try:
            ok = p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest() == tf["sha256"]
        except OSError:
            ok = False
        if not ok:
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(data)
            self.log.write("task_file_restored", s.get("cycle"), path=tf["path"])
        return tf

    def _finish(self, status, reason):
        s = self.state
        s["status"], s["stop_reason"] = status, reason
        self.log.write("task_end", s["cycle"], status=status, reason=reason, model_calls=s.get("calls"))
        self._msg("orchestrator", "user", "final", f"{status}: {reason}", s["cycle"])
        self._save()
        self._say(f"[{status}] {reason} (cycles used: {s['cycle']}{'' if s.get('mode') == 'goal' else '/' + str(s['max_cycles'])})")

    def _halt(self, reason, question=None):
        s = self.state
        s["status"], s["stop_reason"], s["pending_question"] = "HUMAN_REQUIRED", reason, question
        autonomy.bump(s, "owner_interventions")
        self.log.write("human_required", s["cycle"], reason=reason, question=question)
        self._msg("orchestrator", "user", "approval_request" if reason.startswith(("approval_required", "permission_denied")) else "notice",
                  f"HUMAN_REQUIRED: {reason}" + (f"\n{question}" if question else ""), s["cycle"])
        self._save()
        self._say(f"[HUMAN_REQUIRED] {reason}")

    def _limits(self) -> dict:
        b = self.state.get("budget")  # goal mode: the persisted, owner-controlled budget
        return {"codex": b["max_codex_calls"], "claude": b["max_claude_calls"]} if b else budget_limits(self.cfg)

    def _remaining(self, agent) -> int:
        return self._limits()[agent] - self.state.setdefault("calls", {"codex": 0, "claude": 0})[agent]

    @staticmethod
    def _continue_note(cycle, instruction):
        note = (f"(Cycle {cycle} was interrupted when the owner's time budget was reached; the budget has since been extended. "
                "Your work so far is on disk. Continue the SAME assignment and finish it.)\n")
        return instruction if str(instruction or "").startswith("(Cycle ") else note + str(instruction or "")

    def _budget_pause(self, reason: str):
        """Goal mode: a budget is reached -> PAUSE with everything kept; only the owner can extend it, then resume."""
        s = self.state
        s["status"], s["stop_reason"] = "PAUSED", f"budget_reached: {reason}"
        autonomy.bump(s, "owner_interventions")
        self.log.write("budget_reached", s["cycle"], reason=reason, budget=s.get("budget"), calls=s.get("calls"),
                       elapsed_sec=s.get("elapsed_sec"))
        self._msg("orchestrator", "user", "approval_request",
                  f"BUDGET_REACHED: {reason}. The task is paused with all state kept. To continue, the owner extends the budget "
                  f"(browser: 'הגדל תקציב והמשך', or: orchestrator.py extend-budget ...) and resumes.", s["cycle"])
        self._save()
        self._say(f"[PAUSED] budget reached: {reason}")

    def _spend(self, agent) -> bool:
        """Counts a model call BEFORE it is made (persisted, so resume cannot reset the budget)."""
        if self._remaining(agent) <= 0:
            if self.state.get("budget"):
                self._budget_pause(f"{agent} calls")
                return False
            lim = self._limits()[agent]
            self._finish("NEEDS_REVISION" if self.state["cycle"] else "FAILED",
                         f"call_budget_exhausted: {agent} used {lim}/{lim} model calls")
            return False
        self.state["calls"][agent] += 1
        self._save()
        return True

    @staticmethod
    def _search_meta(agent) -> dict:
        ws = (getattr(agent, "last_meta", None) or {}).get("web_searches")
        return {"web_searches": ws} if ws else {}

    def _interrupted(self):
        """Ctrl+C: nothing is lost - the task becomes PAUSED (resumable; an interrupted file write is recovered on resume)."""
        s = self.state
        if s.get("status") == "RUNNING":
            s["status"], s["stop_reason"] = "PAUSED", "user_interrupt"
            self.log.write("paused", s["cycle"], reason="user_interrupt")
            self._msg("user", "orchestrator", "control", "interrupted by the owner (Ctrl+C); resume with: orchestrator.py resume", s["cycle"])
            self._save()
            self._say("[PAUSED] interrupted; resume with: orchestrator.py resume")

    def _cancel_requested(self):
        return read_control(self.run_dir) == "stop"

    def _call_monitor(self) -> bool:
        """Polled while a model call runs. True = stop the call now. A long or quiet call is NEVER stopped for being long or
        quiet: only the owner's stop, or (goal mode) the owner's overall time budget, ends it. Quiet periods are reported.
        (Any exception here ends the call with the agent's process tree killed - safety.run_process - never orphaned.)"""
        if self._cancel_requested():
            self._interrupt = None
            return True
        s = self.state
        if s.get("mode") == "goal" and self._t0 is not None:
            elapsed = self._elapsed_base + time.time() - self._t0
            if elapsed >= s["budget"]["max_minutes"] * 60:
                self._interrupt = f"time {int(elapsed // 60)}/{s['budget']['max_minutes']} min"
                return True
        quiet = time.time() - self._last_activity
        step = self.cfg.get("quiet_notice_sec", 600)
        if quiet >= step * (self._quiet_notices + 1):
            self._quiet_notices += 1
            rec = {"ts": now(), "source": "orchestrator", "notice": True, "quiet_sec": int(quiet),
                   "text": f"quiet: no observable agent activity for "
                           f"{f'{int(quiet)} s' if quiet < 120 else f'{int(quiet // 60)} min'}. The process is still running "
                           "(it may be reasoning or running a long tool). It is NOT stopped automatically; you can keep "
                           "waiting or stop the task."}
            try:  # display-only
                self.log.write("activity", s.get("cycle"), **rec)
                if self.viewer:
                    self.viewer.activity(rec)
            except Exception:
                pass
        return False

    def _call_started(self):
        self._interrupt, self._last_activity, self._quiet_notices = None, time.time(), 0

    def _apply_call_limits(self):
        """Goal mode: no per-call wall-clock limit for Codex or Claude (a call may legitimately take an hour); the owner's
        overall budgets, stop and pause still apply. Fixed mode keeps the configured per-call timeouts."""
        if self.state.get("mode") == "goal":
            for agent in (self.planner, self.executor, self.reviewer):
                if hasattr(agent, "set_call_timeout"):
                    agent.set_call_timeout(None)

    def _check_control(self) -> bool:
        cmd = read_control(self.run_dir)
        if cmd == "stop":
            clear_control(self.run_dir)
            self._msg("user", "orchestrator", "control", "stop requested", self.state["cycle"])
            self._finish("STOPPED", "user_stop")
            return True
        if cmd == "pause":
            clear_control(self.run_dir)
            self._msg("user", "orchestrator", "control", "pause requested", self.state["cycle"])
            self.state["status"], self.state["stop_reason"] = "PAUSED", "user_pause"
            self.log.write("paused", self.state["cycle"])
            self._save()
            self._say("[PAUSED] resume with: orchestrator.py resume")
            return True
        return False

    @contextmanager
    def _phase(self, agent, role, cycle):
        """Marks WHO is working (real events, never messages): the browser viewer shows 'Codex is working...' from these."""
        self.log.write("agent_start", cycle, agent=agent, role=role)
        self._call_started()
        t0 = time.time()
        try:
            yield
        finally:
            self.log.write("agent_end", cycle, agent=agent, role=role)
            m = self.state.setdefault("metrics", {}).setdefault("by_role", {}).setdefault(f"{agent}:{role}", {"calls": 0, "sec": 0.0})
            m["calls"], m["sec"] = m["calls"] + 1, round(m["sec"] + time.time() - t0, 1)

    def _log_meta(self, adapter, cycle):
        meta = getattr(adapter, "last_meta", None)
        if meta:
            self.log.write("agent_meta", cycle, **meta)

    def _note_ignored(self, role, ignored, perm):
        if ignored:
            self.log.write("untrusted_fields_ignored", self.state["cycle"], role=role, keys=ignored)
        if perm:
            self.log.write("permission_expansion_attempt_ignored", self.state["cycle"],
                           role=role, keys=perm)

    # ------------------------------------------------------------------ lifecycle
    def _open(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log = EventLog(self.run_dir / "events.jsonl", self.redactor)
        self.transcript = Transcript(self.run_dir / "transcript.jsonl", self.redactor,
                                     [self.viewer.message] if self.viewer else [])

    def start(self, task: str) -> dict:
        if not executor_is_trusted(self.executor) and not self.file_spec and not self.local_execution:
            raise OrchestratorError(DYNAMIC_EXECUTION_BLOCKED)
        others = find_active_tasks(self.runs_dir, self.workspace)
        if others:
            raise ActiveTaskExists(f"workspace already has active task(s): {others}; resume or stop them first")
        self._lock = acquire_lock(self.runs_dir, self.workspace, self.task_id)
        try:
            self._open()
            if len(task) > LONG_TASK_CHARS and not self.file_spec:  # restricted profile: Claude has no tools to read it
                # in the owner's existing project the file is per task and created exclusively: never overwrites a project file
                name = f"TASK-{self.task_id}.md" if handoff.is_existing({"project": self.project}) else "TASK.md"
                tp = self.workspace / TASK_DIR / name
                tp.parent.mkdir(exist_ok=True)
                with open(tp, "xb" if name != "TASK.md" else "wb") as f:
                    f.write(task.encode("utf-8"))
                self._pending_task_file = {"path": f"{TASK_DIR}/{name}", "sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
                                           "chars": len(task)}
            self.baseline = self._snap()
            atomic_write_json(self.run_dir / "baseline.json", self.baseline)
            self.state = {
                "version": 1, "task_id": self.task_id, "task": task, "workspace": str(self.workspace),
                "status": "RUNNING", "stop_reason": None, "cycle": 0, "max_cycles": self.cfg["max_cycles"],
                "plan": None, "acceptance_criteria": [], "instruction": None, "instruction_source": "codex",
                "executor_session_id": None, "no_progress_streak": 0,
                "last_fingerprint": self._fingerprint(self.baseline, "not_run", None),
                "history": [], "approvals": [], "pending_question": None,
                "config_sha256": hashlib.sha256(json.dumps(self.cfg, sort_keys=True).encode()).hexdigest(),
                "created_at": now(), "updated_at": now(),
                "agents": {"planner": type(self.planner).__name__, "executor": type(self.executor).__name__,
                           "reviewer": type(self.reviewer).__name__},
                "file_spec": self.file_spec, "local_execution": bool(self.local_execution and not self.file_spec),
                "agent_settings": {"codex_model": self.cfg["codex"]["model"], "codex_effort": self.cfg["codex"]["reasoning_effort"],
                                   "claude_model": self.cfg["claude"]["model"], "claude_effort": self.cfg["claude"]["effort"]},
                "approved_tools": [], "pending_permissions": None,
                "mode": self.goal["mode"], "kind": self.goal["kind"], "budget": self.goal["budget"], "elapsed_sec": 0.0,
                "criteria_status": None, "stall": {"instructions": [], "failures": []},
                "task_file": getattr(self, "_pending_task_file", None),
                "project": self.project, "autonomy": self.autonomy,
                "metrics": {"owner_interventions": 0},
            }
            self.log.write("task_start", 0, task=task, workspace=str(self.workspace),
                           env_names_removed_for_children=self.removed_env)
            self._t0 = time.time()
            self._apply_call_limits()
            self._save()
            self._say(f"task {self.task_id} started; workspace {self.workspace}")
            self._msg("user", "codex", "task", task, 0)
            try:
                self._loop()
            except KeyboardInterrupt:
                self._interrupted()
            except Exception as e:
                self._internal_error(e)
                raise
        finally:
            release_lock(self._lock, self.task_id)
        return self.state

    def resume(self) -> dict:
        if not self.state_path.exists():
            raise OrchestratorError(f"no such task: {self.task_id}")
        self._lock = acquire_lock(self.runs_dir, self.workspace, self.task_id)
        try:
            self._open()
            s = self.state = read_json(self.state_path)
            if os.path.normcase(os.path.realpath(s["workspace"])) != os.path.normcase(str(self.workspace)):
                raise OrchestratorError("workspace does not match the task state")
            if handoff.is_existing(s) and os.path.normcase(os.path.realpath(s["project"]["root"])) != os.path.normcase(str(self.workspace)):
                raise OrchestratorError("the workspace is not the task's authorized project root")
            if handoff.handed_off(s):
                raise OrchestratorError("this task was handed off to " + s["handed_off_to"][-1]["task_id"] + "; continue that task instead")
            self.project = s.get("project")
            recover = format_recoverable(s)
            if s["status"] not in ACTIVE and not recover:
                raise OrchestratorError(f"task is {s['status']} and cannot be resumed")
            if not executor_is_trusted(self.executor) and not s.get("file_spec") and not s.get("local_execution"):
                raise OrchestratorError(DYNAMIC_EXECUTION_BLOCKED)
            if any(a["status"] == "pending" for a in s["approvals"]):
                raise OrchestratorError("an approval is pending: run approve (or --deny) first")
            self.baseline = read_json(self.run_dir / "baseline.json")
            self.policy.approved = {(a["op"], a["path"]) for a in s["approvals"] if a["status"] == "approved"}
            was = s.get("stop_reason") or ""
            s["status"], s["stop_reason"], s["pending_question"] = "RUNNING", None, None
            s.pop("pending_decision", None)
            if not was.startswith(("permission_denied", "capability_approval_required")):
                s["pending_permissions"] = None  # a request the owner moved past (e.g. by a continuation) is not left behind
            self._restore_recorded_approvals()  # never ask twice for an exact script version the owner already approved
            # the owner's resume is a new direction: the bounded review-loop counters start again (still bounded)
            s["polish"], s["single_pass_rounds"] = {"rounds": 0, "met": -1}, 0
            s.pop("last_review_key", None)
            # goal mode: the persisted, owner-extended budget applies - a resume never resets or widens it
            if not s.get("budget"):
                s["max_cycles"] = self.cfg["max_cycles"]
            if recover and was.startswith("reviewer_error"):
                s["review_pending"] = self._last_report(s)
            elif recover:
                s["review_pending"] = self._recovered_report(s)
            elif s.get("kind") == "research" and (was.startswith("reviewer_requested_human") or "during Codex's review" in was):
                s["review_pending"] = True  # continue with Codex's review of the existing work; Claude is not re-run
            elif (was.startswith("reviewer_requested_human") and s.get("continuation_pending") is None
                  and autonomy.policy(s)["codex_decides_technical"]):
                # the owner resumed a question under a delegating policy: Codex re-decides it first, on the existing work
                s["review_pending"] = dict(self._last_report(s), reason="policy_rereview")
                autonomy.record(s, "policy_reask", "the owner resumed Codex's question under the "
                                f"{autonomy.policy(s)['level']} policy; Codex re-reviews the existing work and decides",
                                "technical questions are settled by the agents under this policy")
            if was.startswith("stalled"):  # the owner looked at the stall and chose to continue
                s["no_progress_streak"], s["stall"] = 0, {"instructions": [], "failures": []}
            self._elapsed_base, self._t0 = float(s.get("elapsed_sec") or 0.0), time.time()
            self._apply_call_limits()
            ie = self.run_dir / "internal_error.json"
            if ie.exists():
                try:
                    self.log.write("internal_error_seen", s["cycle"], record=json.loads(ie.read_text(encoding="utf-8")))
                    ie.unlink()
                except (OSError, ValueError):
                    pass
            self.log.write("task_resume", s["cycle"])
            if s.get("file_spec"):  # an interrupted write is completed or rolled back BEFORE anything else happens
                notes = self._applier().recover()
                if notes:
                    self.log.write("apply_recovered", s["cycle"], notes=notes)
                    self._msg("orchestrator", "user", "file_apply", {"status": "recovered", "reasons": notes, "written": []}, s["cycle"])
            self._save()
            self._say(f"task {self.task_id} resumed at cycle {s['cycle']}")
            try:
                self._loop()
            except KeyboardInterrupt:
                self._interrupted()
            except Exception as e:
                self._internal_error(e)
                raise
        finally:
            release_lock(self._lock, self.task_id)
        return self.state

    def _loop(self):
        s = self.state
        if s.get("continuation_pending") is not None:  # the owner reopened the task: Codex first plans the continuation
            if (s.get("continuations") or [{}])[-1].get("single_pass"):
                self._start_single_pass()  # the owner's own bounded revision: no planning call
            elif not self._do_plan(continuation=True):
                return
        elif not s["plan"] and not self._do_plan():
            return
        while s["status"] == "RUNNING":
            if self._check_control():
                return
            if s.get("mode") == "goal":
                reason = budget_stop(s)
                if reason:
                    return self._budget_pause(reason)
                if checkpoint_due(s):  # a progress report, not a stop: work continues while it is going normally
                    summary = checkpoint_summary(s)
                    s["last_checkpoint"] = s["cycle"]
                    self.log.write("checkpoint", s["cycle"], **summary)
                    self._msg("orchestrator", "user", "checkpoint", summary, s["cycle"])
                    self._save()
            elif s["cycle"] >= s["max_cycles"]:
                self._finish("NEEDS_REVISION" if s["cycle"] else "FAILED", "max_cycles_reached")
                return
            if self._remaining("claude") <= 0 or self._remaining("codex") <= 0:  # a cycle needs one call of each
                agent = "claude" if self._remaining("claude") <= 0 else "codex"
                self._finish("NEEDS_REVISION", f"call_budget_exhausted: no {agent} call left for another cycle")
                return
            self._cycle()
            if s["status"] == "RUNNING" and self.after_cycle:
                self.after_cycle(self, s["cycle"])

    def _agent_task(self) -> str:
        """The task as Codex sees it: the ORIGINAL request, unchanged, followed by the owner's continuation instructions,
        each clearly labelled (never a silent rewrite)."""
        s = self.state
        out = s["task"]
        if handoff.is_existing(s):  # no path: Codex never receives local paths (see codex_adapter)
            out += ("\n\n---\nWORKING FOLDER (set by the orchestrator): the owner's EXISTING, already deployed project, which the owner "
                    "authorized as the only working root. It is the authoritative version; the implementation agent works directly "
                    "in it, and every file listing, change and test result you receive is read from it by the orchestrator. Prefer "
                    "focused, incremental changes; never ask to copy, restore or synchronize files from any other folder.")
        for c in s.get("continuations") or []:
            out += (f"\n\n---\nOWNER'S CONTINUATION #{c['n']} ({c['at'][:10]}) - the owner reopened this task after it stopped "
                    f"(previous status {c['prior'].get('status')}"
                    + (f", previous research verdict {c['prior']['verdict']}" if c["prior"].get("verdict") else "") + "). "
                    "The previous outcome stays in the history but does NOT end this continuation: address it with new work "
                    "before concluding.\n"
                    + (c["note"] if c.get("note") else "(No additional instructions: continue the unfinished work; where new "
                       "evidence could change the previous conclusion, re-examine it. Do not simply repeat the previous final answer.)"))
        return out

    def _owner_notes(self) -> list:
        return [{"n": c["n"], "at": c["at"], "note": c.get("note") or ""} for c in self.state.get("continuations") or []]

    def _continuation_ctx(self) -> dict:
        """What Codex needs to plan a continuation, rebuilt from the persisted record (Codex calls are stateless)."""
        s = self.state
        c = (s.get("continuations") or [])[-1]
        hist = [{k: h.get(k) for k in ("cycle", "decision", "verification", "findings")} for h in (s.get("history") or [])[-6:]]
        return {"n": c["n"], "note": c.get("note") or "", "prior": c["prior"], "previous_plan": s.get("plan"),
                "previous_acceptance_criteria": s.get("acceptance_criteria"), "previous_criteria_status": s.get("criteria_status"),
                "latest_status_summary": (s.get("status_summary") or {}).get("sections"), "recent_history": hist,
                "cycles_done": s.get("cycle"), "remaining_budget": c.get("remaining")}

    def _start_continuation(self, d) -> None:
        """Apply Codex's continuation plan: the ORIGINAL criteria stay; Codex's new criteria are added (capped)."""
        s = self.state
        c = (s.get("continuations") or [])[-1]
        crit = list(s.get("acceptance_criteria") or [])
        for x in d["acceptance_criteria"]:
            if x not in crit:
                crit.append(x)
        from agents import MAX_CRITERIA
        s["acceptance_criteria"], s["criteria_status"] = crit[:MAX_CRITERIA], None
        s["plan"], s["instruction"], s["instruction_source"] = d["plan"], d["instruction"], "codex"
        s["no_progress_streak"], s["stall"] = 0, {"instructions": [], "failures": []}
        sid, mode = s.get("executor_session_id"), "new_session"
        if sid and hasattr(self.executor, "session_available"):
            ok = self.executor.session_available(sid, str(self.workspace))
            mode = "same_session" if ok else "reconstructed"
            if not ok:
                s["executor_session_id"] = None  # the previous session cannot be resumed: Claude gets the full task + a brief
        elif sid:
            mode = "same_session"
        c.update(planned_cycle=s["cycle"], context=mode, planned_at=now())
        s["continuation_pending"], s["continuation_brief_cycle"] = None, s["cycle"] + 1
        self.log.write("continuation_planned", s["cycle"], n=c["n"], context=mode, criteria_total=len(s["acceptance_criteria"]))
        self._msg("orchestrator", "user", "notice",
                  f"continuation #{c['n']}: Codex planned the next steps; Claude continues "
                  + ("in the SAME session (its previous context is kept)" if mode == "same_session" else
                     "in a NEW session with the full task and a brief of the previous work (the previous session could not be resumed)"
                     if mode == "reconstructed" else "with the full task and a brief of the previous work"), s["cycle"])

    def _start_single_pass(self) -> None:
        """The owner authorized ONE focused revision: Claude works once on the owner's instructions (with Codex's latest
        requested changes as context), Codex reviews once, then the task stops for the owner unless Codex approves."""
        s = self.state
        c = s["continuations"][-1]
        last_request = s.get("instruction") if s.get("instruction_source") == "codex" else None
        s["instruction"] = (("OWNER'S INSTRUCTIONS FOR THIS SINGLE REVISION:\n" + c["note"]) if c.get("note") else
                            "Make the changes requested in Codex's latest review.") + (
            "\n\nCODEX'S LATEST REVIEW REQUESTED (context):\n" + last_request if last_request else "") + (
            "\n\nThis is ONE focused revision pass: change only what is needed, keep earlier evidence, do not start new "
            "broad discovery; afterwards Codex reviews once and the owner decides.")
        s["instruction_source"] = "user"  # the owner (shown as such in the conversation)
        s["no_progress_streak"], s["stall"] = 0, {"instructions": [], "failures": []}
        sid, mode = s.get("executor_session_id"), "new_session"
        if sid and hasattr(self.executor, "session_available"):
            ok = self.executor.session_available(sid, str(self.workspace))
            mode = "same_session" if ok else "reconstructed"
            if not ok:
                s["executor_session_id"] = None
        elif sid:
            mode = "same_session"
        c.update(planned_cycle=s["cycle"], context=mode, planned_at=now())
        s["continuation_pending"], s["continuation_brief_cycle"], s["single_pass"] = None, s["cycle"] + 1, True
        s["single_pass_rounds"] = 0
        self.log.write("single_pass_started", s["cycle"], n=c["n"], context=mode)
        self._msg("orchestrator", "user", "notice",
                  f"continuation #{c['n']} (single pass): Claude revises once, then Codex reviews once; nothing else starts "
                  "automatically. Claude continues " + ("in the SAME session (its previous context is kept)" if mode == "same_session"
                  else "in a NEW session with the full task and a brief of the previous work (the previous session could not be resumed)"
                  if mode == "reconstructed" else "with the full task and a brief of the previous work"), s["cycle"])
        self._save()

    def _do_plan(self, continuation: bool = False) -> bool:
        s = self.state
        self._say("[plan] Codex planner working..." + (" (continuation)" if continuation else ""))
        correction = None
        for attempt in range(PLAN_CORRECTIONS + 1):
            raw = None
            if not self._spend("codex"):  # every correction is a real call and counts against the owner's budget
                return False
            try:
                raw = self._call_model("codex", "planner", 0, lambda: self.planner.plan({"task": self._agent_task(), "workspace": str(self.workspace), "kind": s.get("kind", "code"),
                                             **({"continuation": self._continuation_ctx()} if continuation else {}),
                                             **({"project": handoff.project_overview(
                                                 self.workspace, set(self.cfg["snapshot_ignore_dirs"]) | {TASK_DIR}, self.policy)}
                                                if handoff.is_existing(s) else {}),
                                             **({"file_spec": {k: s["file_spec"][k] for k in ("create", "overwrite", "required_defs")}}
                                                if s.get("file_spec") else {}),
                                             **({"correction": correction} if correction else {})}))
                d, ign, perm = validate_plan(parse_json_object(raw), self.cfg["max_instruction_chars"])
                break
            except AdapterError as e:
                if getattr(e, "halted", False):  # already waiting for the owner (e.g. a model authorization)
                    return False
                if e.kind == "cancelled" and self._interrupt:
                    self._budget_pause(self._interrupt + " (reached during the planner call; it is repeated after resume)")
                    return False
                if raw is not None:
                    self._msg("codex", "orchestrator", "malformed_output", raw, 0)
                # only an answer that arrived but failed OUR format validation is correctable; CLI, auth, quota or policy
                # errors are not retried
                if raw is not None and e.kind == "malformed" and attempt < PLAN_CORRECTIONS:
                    correction = {"error": e.message, "previous_output": raw}
                    self.log.write("plan_correction_requested", 0, attempt=attempt + 1, of=PLAN_CORRECTIONS, error=e.message)
                    self._msg("orchestrator", "codex", "notice", f"plan rejected ({e.message}); correction request "
                              f"{attempt + 1} of {PLAN_CORRECTIONS}", 0)
                    continue
                suffix = f" (after {attempt} correction request{'s' if attempt != 1 else ''})" if attempt else ""
                self._finish(status_for_error(e.kind), self._fatal_text(e, FATAL.get(e.kind, "planner_error")) + suffix)
                return False
        self._note_ignored("planner", ign, perm)
        self._log_meta(self.planner, 0)
        if continuation:
            self._start_continuation(d)
            s["capabilities_needed"] = d.get("capabilities_needed") or []
            self.log.write("plan", s["cycle"], **d)
            self._msg("codex", "orchestrator", "plan", raw, s["cycle"], continuation=s["continuations"][-1]["n"], **self._search_meta(self.planner))
            self._save()
            return self._capability_gate(s["capabilities_needed"])
        s["plan"], s["acceptance_criteria"], s["instruction"] = d["plan"], d["acceptance_criteria"], d["instruction"]
        s["capabilities_needed"] = d.get("capabilities_needed") or []
        self.log.write("plan", 0, **d)
        self._msg("codex", "orchestrator", "plan", raw, 0, **self._search_meta(self.planner))
        self._save()
        return self._capability_gate(s["capabilities_needed"])

    # ------------------------------------------------------------------ one cycle
    @staticmethod
    def _fingerprint(snap, status, ran):
        return hashlib.sha256(f"{snapshot_digest(snap)}|{status}|{ran}".encode()).hexdigest()

    def _verify(self) -> dict:
        argv = list(self.cfg["verify_command"])
        if argv and not os.path.isabs(argv[0]) and Path(argv[0]).stem.lower() in ("python", "python3", "py"):
            argv[0] = sys.executable  # the orchestrator's own interpreter: no dependence on PATH
        self.policy.check_command(argv)
        try:
            r = run_process(argv, cwd=self.workspace, env=self.child_env,
                            timeout=self.cfg["verification_timeout_sec"],
                            cancel_check=self._cancel_requested)
        except ProcessTimeout:
            return {"command": argv, "status": "timeout", "passed": False, "exit_code": None,
                    "tests_ran": None, "output_tail": "", "output_sha256": None}
        out = r.stdout + "\n" + r.stderr
        ran = tests_ran(out)
        m = re.search(r"skipped=(\d+)|\b(\d+) skipped\b", out)
        skipped = int(m.group(1) or m.group(2)) if m else 0
        executed = None if ran is None else ran - skipped  # a skipped test (e.g. a missing optional tool) proves nothing
        need = self.cfg["min_tests_ran"]
        passed = r.returncode == 0 and ((executed is not None and executed >= need) or (ran is None and need == 0))
        status = "passed" if passed else ("no_test_evidence" if r.returncode == 0 else "failed")
        return {"command": argv, "status": status, "passed": passed, "exit_code": r.returncode, "evidence_class": "executed_tests",
                "tests_ran": ran, "tests_skipped": skipped, "output_tail": clip(out[-3000:], 3000),
                "output_sha256": hashlib.sha256(out.encode("utf-8", "replace")).hexdigest()}

    def _posthoc_violations(self, diff) -> list[str]:
        v = []
        for rel in diff["added"] + diff["modified"] + diff["deleted"]:
            if self.policy.is_protected(rel):
                v.append(f"protected path changed: {rel}")
        for rel in diff["deleted"]:
            if self.cfg["deletes_require_approval"] and ("delete", rel) not in self.policy.approved:
                v.append(f"unapproved deletion: {rel}")
        return v

    def _need_approval(self, e: ApprovalRequired):
        s = self.state
        a = next((x for x in s["approvals"] if (x["op"], x["path"], x["status"]) == (e.op, e.path, "pending")), None)
        if not a:
            a = {"id": f"a{len(s['approvals']) + 1}", "op": e.op, "path": e.path, "status": "pending"}
            s["approvals"].append(a)
        self._halt(f"approval_required:{a['id']}", f"Approve {e.op} of '{e.path}'? "
                   f"run: orchestrator.py approve --approval-id {a['id']} [--deny]")

    def _applier(self):
        return ProposalApplier(self.policy, GuardedFS(self.policy, self.child_env, self.cfg["process_timeout_sec"]),
                               self.run_dir, self.state["file_spec"], self.redactor)

    def _apply_proposal(self, files, deletions, c) -> dict:
        """The ORCHESTRATOR (never the agent) validates the proposed files and writes only allowlisted ones."""
        with self._phase("orchestrator", "apply", c):
            rec = apply_step(self._applier(), files, deletions, c)
        self.log.write("file_apply", c, **rec)
        self._msg("orchestrator", "codex", "file_apply", rec, c)
        self._say(f"[cycle {c}] file proposal {rec['status']}: " + (", ".join(w["path"] for w in rec["written"]) or "; ".join(rec["reasons"])[:300]))
        self._save()
        return rec

    def _cycle(self):
        s, cfg = self.state, self.cfg
        proposal = bool(s.get("file_spec"))
        pending = s.pop("review_pending", None)
        # research: Codex reviews the existing material; any kind: a technically failed review is retried alone
        review_only = bool(pending) and (s.get("kind") == "research" or isinstance(pending, dict))
        # the owner's "retry the independent review only": the review runs and NOTHING else - every outcome but approval
        # waits for the owner (no Claude call, no new research cycle)
        retry_only = review_only and isinstance(pending, dict) and pending.get("reason") == "review_retry"
        single_pass = bool(s.get("single_pass")) and not review_only  # the owner's one-revision continuation
        s["cycle"] += 1
        c = s["cycle"]
        self._save()
        self._say(f"[cycle {c}{'' if s.get('mode') == 'goal' else '/' + str(s['max_cycles'])}] "
                  + ("Codex review of the existing work (no Claude call)" if review_only else "Claude executor working..."))
        before = self._snap()
        cap_offset = self._audit_offset()
        exc, report, raw, files, dels, applied = None, None, None, [], [], None
        if review_only:
            self._msg("orchestrator", "codex", "notice", "Review-only continuation: Codex reviews the existing report and research "
                      "files; Claude is not called in this cycle.", c)
            report = {"summary": "(review-only cycle: no new work by the implementation agent)", "changed_files": [],
                      "commands_run": [], "remaining_issues": [], "session_id": None, "turns": 0, "sources": []}
            if isinstance(pending, dict) and pending.get("report"):  # the report of the previous cycle, re-validated
                report = dict(pending["report"], changed_files=[])  # its changes are already part of the complete material
        else:
            ctx = {"task": s["task"], "workspace": str(self.workspace), "cycle": c, "plan": s["plan"], "owner_notes": self._owner_notes(),
                   "acceptance_criteria": s["acceptance_criteria"], "instruction": s["instruction"],
                   "session_id": s["executor_session_id"], "limits": {"max_turns": cfg["max_executor_turns"]},
                   "instruction_source": s.get("instruction_source", "codex"), "task_file": self._task_file(),
                   "project_root": str(self.workspace) if handoff.is_existing(s) else None}
            fs = NoToolsFS(self.workspace) if proposal else GuardedFS(self.policy, self.child_env, cfg["process_timeout_sec"])
            ctx["kind"] = s.get("kind", "code")
            ctx["max_cycles"] = "open-ended (goal mode: until the goal is verified or a budget is reached)" if s.get("mode") == "goal" else s["max_cycles"]
            ctx["previous_evidence"] = s.get("last_evidence")  # failure details reach the executor from the record itself
            if s.get("continuation_brief_cycle") == c:  # first step of a continuation: what was done before, from the record
                cont = (s.get("continuations") or [{}])[-1]
                ctx["continuation_brief"] = {"n": cont.get("n"), "prior": cont.get("prior"), "context": cont.get("context"),
                                             "latest_status_summary": (s.get("status_summary") or {}).get("sections")}
            ctx["extra_allowed_tools"] = list(s.get("approved_tools") or [])  # rules the OWNER approved for this task only
            if not proposal:  # this task's capability policy, regenerated from the owner-authorized state every cycle
                ctx["capability_policy"] = self._write_cap_policy(c)
                ctx["agent_results"] = self._agent_results_for(c)
            if proposal:
                ctx["file_spec"] = {k: s["file_spec"][k] for k in ("create", "overwrite", "required_defs")}
                ctx["current_files"] = current_files(self.workspace, s["file_spec"], self.redactor)
            meta = {}
            if hasattr(self.executor, "describe_prompt"):  # the full prompt the real CLI will receive (redacted)
                meta["delivered_prompt"] = self.executor.describe_prompt(ctx)
            self._msg(s.get("instruction_source", "codex"), "claude", "instruction", s["instruction"], c, **meta)
            self._spend("claude")
            call_ctx, attempt, redirects = ctx, 0, 0
            while True:
                exc, report, raw = None, None, None
                try:
                    raw = self._call_model("claude", "executor", c, lambda: self.executor.execute(call_ctx, fs))
                    turns = None if s.get("mode") == "goal" else cfg["max_executor_turns"]  # goal mode: turns are recorded, not capped
                    if proposal:
                        report, files, dels, ign, perm = validate_proposal_report(parse_json_object(raw), turns)
                    else:
                        report, ign, perm = validate_report(parse_json_object(raw), turns)
                    self._note_ignored("executor", ign, perm)
                except (ApprovalRequired, PolicyViolation, AdapterError) as e:
                    exc = e
                self._log_meta(self.executor, c)  # also after a rejected report: the call happened and used the subscription
                meta_sid = (getattr(self.executor, "last_meta", None) or {}).get("session_id")
                if report is None and meta_sid:
                    s["executor_session_id"] = meta_sid  # a rejected report must not silently start a NEW Claude session
                denied = self._redirectable(exc, redirects)
                if denied:  # routine commands blocked by the rules: the same session is pointed at the gateway (no owner)
                    redirects += 1
                    note = autonomy.redirect_note(denied, CAPGATE_CMD)
                    self._decide("redirect", "blocked shell command(s) redirected to the capability gateway: "
                                 + "; ".join(str(d.get("input"))[:160] for d in denied),
                                 "a routine test/script/diagnostic composition the gateway covers; nothing was granted")
                    self._spend("claude")
                    call_ctx = dict(ctx, session_id=meta_sid, instruction=note + "\n\nPrevious instruction:\n" + str(s["instruction"]),
                                    instruction_source="orchestrator")
                    continue
                # a report that arrived but failed FORMAT validation: the same session resends only a corrected report;
                # its research and file changes are kept and never redone. Content is never judged or invented here.
                if (proposal or raw is None or not meta_sid or not isinstance(exc, AdapterError) or exc.kind != "malformed"):
                    break
                if attempt == REPORT_CORRECTIONS:
                    exc.message += f" (after {attempt} format correction request{'s' if attempt != 1 else ''})"
                    break
                if attempt == REPORT_CORRECTIONS:
                    exc.message += f" (after {attempt} format correction request{'s' if attempt != 1 else ''})"
                    break
                self._msg("claude", "orchestrator", "malformed_output", raw, c)
                self.log.write("report_correction_requested", c, attempt=attempt + 1, of=REPORT_CORRECTIONS, error=exc.message)
                self._msg("orchestrator", "claude", "notice", f"Claude's report was rejected for its FORMAT, not its content "
                          f"({exc.message}). Correction request {attempt + 1} of {REPORT_CORRECTIONS}: the same session resends "
                          "only the corrected report; nothing is researched again.", c)
                if not self._spend("claude"):  # the owner's Claude budget: paused, the work on disk is kept
                    if s.get("kind") == "research":
                        s["review_pending"] = True
                        self._save()
                    return
                call_ctx = dict(ctx, session_id=meta_sid, format_correction={"error": exc.message})
                attempt += 1
        if report is not None and not review_only and report.get("status_summary"):
            self._set_summary(report["status_summary"], "claude", c)  # interim: not yet reviewed by Codex
        if raw is not None:
            if report is not None:
                self._msg("claude", "codex", "report", raw, c)
            else:
                self._msg("claude", "orchestrator", "malformed_output", raw, c)
        if proposal and report is not None and exc is None:
            applied = self._apply_proposal(files, dels, c)
        after = self._snap()
        diff, cumulative = diff_snapshots(before, after), diff_snapshots(self.baseline, after)
        self.log.write("executor_result", c, instruction=s["instruction"], report=report,
                       error=None if exc is None else f"{type(exc).__name__}: {exc}", diff=diff)
        violations = self._posthoc_violations(diff)
        if proposal:  # in proposal mode the ONLY permitted changes are the files the orchestrator itself just wrote
            written = {w["path"] for w in (applied or {}).get("written", [])}
            violations += [f"changed outside the accepted proposal: {p}" for p in diff["added"] + diff["modified"] + diff["deleted"]
                           if p not in written]
        if violations:
            return self._halt("posthoc_policy_violation: " + "; ".join(violations))
        caps = self._cap_records(cap_offset)  # what Claude ran through the capability gateway in this cycle (audited)
        if any(r.get("decision") == "needs_owner" for r in caps):
            return self._capability_halt(caps)
        if getattr(exc, "halted", False):  # already waiting for the owner (model authorization)
            return self._save()
        ops = self._run_agent_ops(c, report) if (report is not None and exc is None and not proposal and not review_only) else []
        if s["status"] != "RUNNING":
            return self._save()
        if isinstance(exc, ApprovalRequired):
            return self._need_approval(exc)
        if isinstance(exc, PolicyViolation):
            return self._halt(f"policy_violation: {exc}")
        if isinstance(exc, AdapterError) and exc.kind == "permission":
            meta = getattr(self.executor, "last_meta", None) or {}
            if meta.get("session_id"):
                s["executor_session_id"] = meta["session_id"]  # the same Claude session continues after your decision
            s["pending_permissions"] = meta.get("denials") or [{"tool": exc.message[:80], "input": "", "suggested_rule": ""}]
            rules = [d["suggested_rule"] for d in s["pending_permissions"] if d.get("suggested_rule")]
            return self._halt(f"permission_denied: {exc.message}",
                              "Claude asked for something that is not pre-approved: " + (", ".join(rules) or exc.message)
                              + ". Nothing was approved automatically. Approve it for THIS task only (answer in the start "
                              "window, or: orchestrator.py allow-tool [--rule RULE] [--deny]), then resume.")
        if isinstance(exc, AdapterError) and exc.kind == "cancelled" and self._interrupt:
            s["history"].append({"cycle": c, "executor": "interrupted", "verification": "not_run", "tests_ran": None,
                                 "reviewer": None, "decision": "PAUSED", "progress": bool(any(diff.values())),
                                 "findings": [f"the owner's budget was reached during Claude's call ({self._interrupt}); "
                                              "work on disk and the Claude session are kept"]})
            s["instruction"] = self._continue_note(c, s["instruction"])
            return self._budget_pause(self._interrupt + " (reached during Claude's call; it continues after resume)")
        if isinstance(exc, AdapterError) and exc.kind in FATAL:
            return self._finish(status_for_error(exc.kind), self._fatal_text(exc))
        exec_failed = exc is not None
        if exec_failed:
            self._msg("orchestrator", "codex", "notice", f"executor failed ({exc.kind}): {exc.message}", c)
        if report and report["session_id"]:
            s["executor_session_id"] = report["session_id"]

        if exec_failed:
            ver = {"status": "not_run", "passed": False, "tests_ran": None}
        elif proposal:
            if applied["status"] != "applied":
                ver = not_run_evidence(f"nothing was written (proposal {applied['status']}), so nothing was checked")
            else:
                with self._phase("orchestrator", "static_check", c):
                    ver = static_evidence(self.workspace, s["file_spec"]["applied"], s["file_spec"]["required_defs"])
        elif s.get("kind") == "research":  # research: the report's structure and evidence are checked; nothing is executed
            with self._phase("orchestrator", "static_check", c):
                ver = research_evidence(self.workspace, s["task"])
        else:
            if not executor_is_trusted(self.executor) and not s.get("local_execution"):  # only with the owner's acceptance
                raise OrchestratorError(DYNAMIC_EXECUTION_BLOCKED)
            self._say(f"[cycle {c}] independent verification: {' '.join(cfg['verify_command'])}")
            try:
                with self._phase("orchestrator", "verification", c):
                    ver = self._verify()
            except ProcessCancelled:
                return self._finish("STOPPED", "user_stop")
            except PolicyViolation as e:
                return self._finish("FAILED", f"verification_command_not_allowed: {e}")
        gs = None if exec_failed else git_status(self.workspace, self.child_env)
        evidence = {"diff": diff, "cumulative_diff": cumulative, "verification": ver, "git_status": gs}
        if caps:
            evidence["capability_runs"] = self._cap_evidence(caps)
        if ops:
            evidence["agent_operations"] = [{k: o.get(k) for k in ("id", "op", "artifact", "status", "verdict", "findings",
                                                                  "corrections", "reason", "sha256")} for o in ops]
        material = None
        if proposal:
            evidence["file_apply"] = applied
        elif s.get("kind") == "research" and not exec_failed:
            material = research_review.prepare(self.workspace, self._material_budget())
            evidence["research_material_coverage"] = material["manifest"]  # complete texts go to Codex, not into the transcript
        elif not exec_failed:
            evidence["changed_files"] = changed_file_contents(self.workspace, cumulative if review_only else diff, self.redactor)
        s["last_evidence"] = compact_evidence(c, evidence)
        self.log.write("verification", c, **evidence)
        self._msg("orchestrator", "codex", "verification", evidence, c)
        self._say(f"[cycle {c}] verification: {ver['status']} (tests ran: {ver.get('tests_ran')}); "
                  f"files +{len(diff['added'])} ~{len(diff['modified'])} -{len(diff['deleted'])}")
        if exec_failed and isinstance(exc, AdapterError) and exc.kind in ("process", "timeout"):
            s["history"].append({"cycle": c, "executor": f"error:{exc.kind}", "verification": ver["status"], "tests_ran": None,
                                 "reviewer": None, "decision": "FAILED", "progress": False,
                                 "findings": [f"executor failed ({exc.kind}): {exc.message}"]})
            changed = diff["added"] + diff["modified"] + diff["deleted"]
            return self._finish("FAILED", f"executor_error: {exc.message}"
                                + (f"; files changed but NOT verified: {', '.join(changed[:10])}" if changed else ""))

        fp = self._fingerprint(after, ver["status"], ver.get("tests_ran"))
        no_change = not any(diff.values())
        progress = fp != s["last_fingerprint"] and not (exec_failed and no_change)
        if not (exec_failed and no_change):
            s["last_fingerprint"] = fp

        rv, decision, findings, instruction = None, "CONTINUE", [], s["instruction"]
        instr_src = s.get("instruction_source", "codex")
        if exec_failed:
            findings = [f"executor failed ({exc.kind}): {exc.message}"]
        else:
            claimed = {p.replace("\\", "/") for p in report["changed_files"]}  # review-only: [] (no new changes this cycle)
            actual = set(diff["added"] + diff["modified"] + diff["deleted"])
            discrepancy = {"claimed_not_changed": sorted(claimed - actual),
                           "changed_not_claimed": sorted(actual - claimed)}
            if any(discrepancy.values()):
                self.log.write("report_discrepancy", c, **discrepancy)
            self._say(f"[cycle {c}] Codex reviewer working...")
            raw_r = None
            try:
                rev_evidence = evidence
                if material is not None:  # the COMPLETE research material; parts beyond one call are reviewed first
                    rm = self._review_parts(c, material)
                    if rm is None:
                        return  # the owner's Codex budget was reached between parts: paused, the review resumes later
                    rev_evidence = dict(evidence, research_material=rm)
                review_ctx = {"task": self._agent_task(), "plan": s["plan"], "cycle": c, "acceptance_criteria": s["acceptance_criteria"],
                              "executor_report_UNTRUSTED": report, "report_discrepancy": discrepancy,
                              "evidence": rev_evidence, "previous_status_summary": (s.get("status_summary") or {}).get("sections")}
                if not self._spend("codex"):
                    if s.get("kind") == "research":
                        s["review_pending"] = True
                        self._save()
                    return
                raw_r, rv, route, route_why = self._review_with_policy(c, review_ctx)
                s["review_auto_retries"] = 0
                if rv.get("criteria_status") is not None:
                    s["criteria_status"] = rv["criteria_status"]  # Codex's view; the completion gate also requires all 'met'
                if rv.get("status_summary"):
                    self._set_summary(rv["status_summary"], "codex", c, final=route == "complete")
                self._log_meta(self.reviewer, c)
                self._msg("codex", "claude" if route == "continue" else "user", "review", raw_r, c, outcome=autonomy.outcome_of(rv),
                          route=route, **self._search_meta(self.reviewer))
            except AdapterError as e:
                raw_r = getattr(e, "raw", raw_r)
                if getattr(e, "halted", False):  # already waiting for the owner (model authorization); work is kept
                    if s.get("kind") == "research" or self._pol()["review_failure_pauses"]:
                        s["review_pending"] = {"reason": "auto_review_retry", "cycle": c, "report": report}
                    return self._save()
                if e.kind == "cancelled" and self._interrupt:
                    s["history"].append({"cycle": c, "executor": "error:" + exc.kind if exec_failed else "ok",
                                         "verification": ver["status"], "tests_ran": ver.get("tests_ran"), "reviewer": None,
                                         "decision": "PAUSED", "progress": progress,
                                         "findings": [f"the owner's budget was reached during Codex's review ({self._interrupt})"]})
                    s["instruction"] = self._continue_note(c, s["instruction"])
                    if s.get("kind") == "research":
                        s["review_pending"] = True
                    return self._budget_pause(self._interrupt + " (reached during Codex's review)")
                if raw_r is not None:
                    self._msg("codex", "orchestrator", "malformed_output", raw_r, c)
                if e.kind in FATAL and not (e.kind == "malformed" and self._pol()["review_failure_pauses"]):
                    return self._finish(status_for_error(e.kind), self._fatal_text(e))
                # never an approval; Claude's work is intact on disk: pause (or, if the owner's policy allows, retry) the
                # review alone - Claude is not re-run (code tasks included)
                if s.get("kind") == "research" or self._pol()["review_failure_pauses"]:
                    return self._review_failed(c, e, report, ver, progress)
                return self._finish("FAILED", f"reviewer_error ({e.kind}): {e.message}")  # legacy tasks: no correction without a review
            if rv:
                findings = rv["findings"]
                decision = {"complete": "COMPLETE", "continue": "CONTINUE", "owner": "HUMAN_REQUIRED", "failed": "FAILED"}[route]
                if decision == "HUMAN_REQUIRED":
                    od = rv.get("owner_decision") or {}
                    s["pending_decision"] = {"question": rv.get("question") or od.get("action") or "", "why_owner": route_why, **od}
                    rv["question"] = s["pending_decision"]["question"]
                if decision == "COMPLETE":
                    ok, why = self._completion_gate(ver, cumulative)
                    only_unverified = False
                    if ok:
                        ok, why, only_unverified = self._criteria_gate(rv)
                    s["gate_unverified_streak"] = (s.get("gate_unverified_streak", 0) + 1) if (not ok and only_unverified) else 0
                    if not ok and s["gate_unverified_streak"] >= 2:  # cannot be demonstrated here: the owner decides
                        s["pending_decision"] = {"question": f"Acceptance cannot be demonstrated in this environment: {why}. Accept the "
                                                 "work as it is, or provide what is needed to verify it?", "category": "acceptance",
                                                 "why_owner": "only the owner can accept unverified acceptance criteria"}
                        return self._halt(f"acceptance_unverifiable: {why}", s["pending_decision"]["question"])
                    if ok and (rv.get("nonblocking") or autonomy.outcome_of(rv) == "minor_only"):
                        s["nonblocking_suggestions"] = rv.get("nonblocking") or ([] if rv["verdict"] == "COMPLETE" else findings)
                        if rv["verdict"] != "COMPLETE":
                            self._decide("accept_minor", "accepted with non-blocking suggestions recorded: "
                                         + "; ".join(s["nonblocking_suggestions"])[:300], route_why)
                    if not ok:
                        self.log.write("completion_rejected", c, why=why)
                        self._say(f"[cycle {c}] COMPLETE rejected: {why}")
                        self._msg("orchestrator", "codex", "notice", f"COMPLETE rejected by deterministic gate: {why}", c)
                        instr_src = "orchestrator"
                        decision = "CONTINUE"
                        findings = findings + [f"completion rejected by deterministic gate: {why}"]
                        instruction = ("Verification does not support completion (" + why + "). "
                                       + ("Fix this in your next file proposal. Problems: " if proposal else
                                          "Fix this and make the project's tests pass. Last output: ")
                                       + clip(ver.get("output_tail", ""), 1500))
                elif decision == "CONTINUE":  # corrections go to Claude automatically (a COMPLETE with material findings too)
                    instruction = rv["next_instruction"] if len(rv["next_instruction"].strip()) >= 10 else (
                        "Address these review findings: " + "; ".join(findings)[:3000])
                    instr_src = "codex"

        s["history"].append({"cycle": c, "executor": "review-only" if review_only else ("error:" + exc.kind if exec_failed else "ok"),
                             "verification": ver["status"], "tests_ran": ver.get("tests_ran"),
                             "reviewer": rv["verdict"] if rv else None, "decision": decision,
                             "progress": progress, "findings": findings[:10]})
        if single_pass and rv is not None:
            s.pop("single_pass", None)  # this was the one review of the owner's single revision
        if decision == "COMPLETE":
            s["instruction"] = None
            if ver.get("mode") == "research":
                if ver.get("verdict") == "INCONCLUSIVE":
                    return self._finish("INCONCLUSIVE", "research finished without sufficient evidence for a conclusion (verdict INCONCLUSIVE)")
                return self._finish("COMPLETE", f"research report verified for structure and evidence presence (not for truth); "
                                                f"verdict {ver.get('verdict')}; reviewer approved")
            return self._finish("COMPLETE", "acceptance verified: static checks passed (generated code was NOT executed) and reviewer approved"
                                if proposal else "acceptance verified: tests passed and reviewer approved")
        if decision == "HUMAN_REQUIRED":
            return self._halt("reviewer_requested_human", rv["question"])
        pol = self._pol()
        if rv is not None and decision == "CONTINUE" and pol.get("terminate_on_stall") and autonomy.repeated_findings(s, rv):
            return self._unresolved(c, "repeated_findings", "Codex returned the same findings and next step again after "
                                    "Claude's correction", findings, instruction)
        if retry_only or (single_pass and rv is not None):
            limit = pol.get("single_pass_corrections", 0)
            if single_pass and not retry_only and decision == "CONTINUE" and limit:
                # ordinary technical feedback within the existing authorization goes back to Claude - a bounded number of
                # times - instead of waiting for the owner
                n = s.get("single_pass_rounds", 0)
                if n >= limit:
                    return self._unresolved(c, "corrections_exhausted", f"Codex still requests changes after {limit} automatic "
                                            "correction round(s) of this revision", findings, instruction)
                s["single_pass"], s["single_pass_rounds"] = True, n + 1
                self._decide("auto_correction", f"Codex's requested changes were sent back to Claude automatically (correction "
                             f"round {n + 1} of {limit})", "ordinary technical feedback inside the authorized mission; "
                             "nothing new was authorized")
            else:  # the owner's explicit review-only retry, a supervised/legacy single revision, or a gate refusal
                return self._review_outcome_waits(c, decision, findings, instruction, instr_src)
        if decision == "FAILED":
            return self._finish("FAILED", "reviewer_declared_failure: " + "; ".join(findings)[:300])
        if rv is not None and s.get("mode") == "goal" and not review_only:  # tests pass, reviews keep polishing: stop
            looping = autonomy.review_loop_reason(s, ver, autonomy.outcome_of(rv), rv.get("criteria_status"), self._pol())
            if looping and pol.get("terminate_on_stall"):
                return self._unresolved(c, "diminishing_returns", looping, findings, instruction)
            if looping:
                s["instruction"], s["instruction_source"] = instruction, instr_src
                s["pending_decision"] = {"question": "The tests pass but the reviews keep requesting further changes without any "
                                         "additional acceptance criterion being met. Accept the current result, or continue with "
                                         "a clarified priority?", "category": "acceptance", "why_owner": looping}
                return self._halt(f"review_loop: {looping}", s["pending_decision"]["question"])
        # a review-only cycle produces no new work by design: it neither counts as progress nor as a stall
        stalled = None if review_only else (stall_reason(s, ver, instruction if instr_src == "codex" else None, progress)
                                            if s.get("mode") == "goal" else None)
        s["instruction"], s["instruction_source"] = instruction, instr_src
        if not review_only:
            s["no_progress_streak"] = 0 if progress else s["no_progress_streak"] + 1
        self._save()
        if stalled and pol.get("terminate_on_stall"):  # report the blocker and end the loop - no request to continue
            return self._unresolved(c, "stalled", stalled, findings, instruction)
        if stalled:  # goal mode: never loop on a stall - stop and ask the owner
            return self._halt(f"stalled: {stalled}", "The agents are not making measurable progress. Review the conversation, "
                              "then resume (optionally with a clarified task) or stop.")
        if s.get("mode") != "goal" and s["no_progress_streak"] >= cfg["max_no_progress"]:
            self._finish("FAILED", f"no_progress: {s['no_progress_streak']} consecutive cycles without progress")

    def _unresolved(self, c, kind: str, why: str, findings: list, instruction) -> None:
        """Bounded autonomy reached its limit: END the loop with a concrete blocker report (no owner question, no further model
        calls). History, files, budgets, approvals and Codex's last requested step are kept; the owner may continue later."""
        s = self.state
        crit, cs = s.get("acceptance_criteria") or [], s.get("criteria_status") or []
        s["instruction"], s["instruction_source"] = instruction, "codex"
        s["blocker"] = {"kind": kind, "why": str(why)[:600], "cycle": c, "open_findings": [str(f)[:500] for f in findings[:10]],
                        "criteria_open": [t for i, t in enumerate(crit) if i < len(cs) and cs[i] != "met"][:20],
                        "next_step": str(instruction or "")[:1500]}
        self.log.write("unresolved_blocker", c, **{k: v for k, v in s["blocker"].items() if k != "cycle"})
        self._finish("NEEDS_REVISION", f"unresolved_blocker: {kind}: {why}"[:600])

    @staticmethod
    def _fatal_text(e, label=None) -> str:
        """The stop reason with the classified diagnosis: what happened and what would be needed to continue."""
        cls = getattr(e, "failure", None) or autonomy.classify_failure(e.kind, e.message)
        return (f"{label or FATAL.get(e.kind, 'agent_error')}: {e.message}"
                + (f" | class: {cls['class']} | to continue: {cls['diagnosis']}" if e.kind in ("auth", "quota", "config") else ""))

    def _set_summary(self, sections, author, cycle, final=False, **extra):
        """Persist the latest AI-authored status summary at once (display-only). Older ones are kept in a short history."""
        s = self.state
        from conversation import _last_seq
        set_status_summary(s, sections, author, cycle, final=final, after_seq=_last_seq(self.run_dir / "transcript.jsonl"), **extra)
        self.log.write("status_summary", cycle, author=author, final=final)
        self._save()

    def _last_report(self, s) -> dict:
        """The implementation agent's last ACCEPTED report (already validated when it arrived), for a retried review."""
        out = {"reason": "review_retry", "cycle": s["cycle"], "report": None}
        try:
            msgs = [json.loads(x) for x in (self.run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
            last = [m for m in msgs if m.get("kind") == "report" and m.get("sender") == "claude"]
            if last:
                out["report"] = validate_report(parse_json_object(last[-1]["content"]), None)[0]
        except (OSError, ValueError, AdapterError) as e:
            out["error"] = str(e)[:300]
        self.log.write("review_retry_prepared", s["cycle"], report_available=out["report"] is not None)
        return out

    def _recovered_report(self, s) -> dict:
        """The executor's last report that was rejected for FORMAT only, re-validated with the current rules (never edited)."""
        out = {"reason": "format_recovery", "cycle": s["cycle"], "report": None}
        try:
            msgs = [json.loads(x) for x in (self.run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
            last = [m for m in msgs if m.get("kind") == "malformed_output" and m.get("sender") == "claude" and m.get("cycle") == s["cycle"]]
            if last:
                out["report"] = validate_report(parse_json_object(last[-1]["content"]), None)[0]
        except (OSError, ValueError, AdapterError) as e:
            out["revalidation_error"] = str(e)[:300]
        self.log.write("recovered_after_format_error", s["cycle"], report_revalidated=out["report"] is not None,
                       error=out.get("revalidation_error"))
        if out["report"] is not None:
            self._msg("claude", "codex", "report", json.dumps(out["report"], ensure_ascii=False), s["cycle"], recovered=True)
        return out

    def _review_outcome_waits(self, c, decision, findings, instruction, instr_src):
        """After a review-only retry: Codex's findings and requested next step are kept; the task PAUSES for the owner's
        explicit decision. Resuming then runs Claude on Codex's requested changes; nothing starts automatically."""
        s = self.state
        kind = "review_rejected" if decision == "FAILED" else "review_changes_requested"
        if decision != "FAILED":
            s["instruction"], s["instruction_source"] = instruction, instr_src  # what would run IF the owner resumes
        s["status"], s["stop_reason"] = "PAUSED", f"{kind}: " + ("; ".join(findings)[:300] or "see the review")
        self.log.write(kind, c, findings=findings[:10])
        self._msg("orchestrator", "user", "notice", f"{kind}: the retried independent review did not approve the research. "
                  "Its findings are kept; nothing else was started. The owner decides what happens next.", c)
        self._save()
        self._say(f"[PAUSED] {s['stop_reason']}")

    def _review_failed(self, c, e, report, ver, progress):
        """Research: the independent review failed TECHNICALLY (not for the content). Nothing is approved; the task PAUSES with
        the research intact and a review-only retry pending (Codex re-reviews; Claude is not re-run; the call is counted)."""
        s = self.state
        msg = f"review_failed ({e.kind}): {e.message}"[:600]
        pol, n = self._pol(), s.get("review_auto_retries", 0)
        if n < pol["review_retries"] and self._remaining("codex") > 0 and e.kind not in ("auth", "quota", "config", "cancelled"):
            s["review_auto_retries"] = n + 1  # the owner's policy allows retrying the REVIEW alone; Claude is not re-run
            s["history"].append({"cycle": c, "executor": "review-only" if report and str(report.get("summary", "")).startswith("(review-only") else "ok",
                                 "verification": ver["status"], "tests_ran": ver.get("tests_ran"), "reviewer": "error",
                                 "decision": "RETRY_REVIEW", "progress": progress,
                                 "findings": [f"the independent review failed technically ({e.kind}); retried alone"]})
            s["review_pending"] = {"reason": "auto_review_retry", "cycle": c, "report": report}
            self._decide("recovery", f"the independent review failed technically ({e.kind}); the review alone is retried "
                         f"({n + 1} of {pol['review_retries']}); Claude's work is kept and not redone",
                         (getattr(e, "failure", None) or {}).get("diagnosis") or e.message[:200])
            self._save()
            return
        s["history"].append({"cycle": c, "executor": "review-only" if report and report.get("summary", "").startswith("(review-only") else "ok",
                             "verification": ver["status"], "tests_ran": ver.get("tests_ran"), "reviewer": "error",
                             "decision": "PAUSED", "progress": progress,
                             "findings": [f"the independent review failed technically ({e.kind}); nothing was approved"]})
        s["review_pending"] = {"reason": "review_retry", "cycle": c, "report": report}
        s["status"], s["stop_reason"] = "PAUSED", msg
        self.log.write("review_failed", c, kind=e.kind, error=e.message[:400])
        self._msg("orchestrator", "user", "notice", f"independent review failed technically ({e.kind}); nothing was approved. "
                  "The work is intact; the review can be retried alone.", c)
        self._save()
        self._say(f"[PAUSED] {msg}")

    def _material_budget(self) -> int:
        """Characters of research material per Codex call: the configured size, never more than the prompt limit allows."""
        fixed = len(self.state["task"]) + len(json.dumps(self.state["acceptance_criteria"], ensure_ascii=False)) + 60_000
        room = int((self.cfg["codex"]["max_prompt_chars"] - fixed) / 1.15)  # JSON escaping of the texts
        return max(10_000, min(self.cfg["review_material_chars"], room))

    def _review_parts(self, c, material):
        """Material larger than one call: Codex reads parts 1..n-1 in separate (counted) calls and writes notes; the final review
        call gets those notes, the last part's text and the coverage manifest. Returns None if the owner's budget paused it."""
        s, parts, man = self.state, material["parts"], material["manifest"]
        notes = []
        for i, part in enumerate(parts[:-1], 1):
            if not self._spend("codex"):
                s["review_pending"] = True
                self._save()
                return None
            ids = [seg["id"] for seg in part]
            with self._phase("codex", "reviewer", c):
                raw = self.reviewer.review_part({"task": s["task"], "acceptance_criteria": s["acceptance_criteria"], "part": i,
                                                 "parts": len(parts), "segments": research_review.part_view(part), "coverage": man})
            n = research_review.validate_part_notes(parse_json_object(raw), ids)
            self._log_meta(self.reviewer, c)
            self.log.write("review_part", c, part=i, parts=len(parts), coverage=n["coverage"])
            self._msg("codex", "orchestrator", "notice", f"Codex reviewed part {i}/{len(parts)} of the research material "
                      f"({ids[0]}-{ids[-1]}; {len(n['coverage']['confirmed'])}/{len(ids)} segments confirmed)", c,
                      **self._search_meta(self.reviewer))
            notes.append(dict(n, part=i))
        self.log.write("review_material", c, parts=len(parts), segments=man["segments"], total_chars=man["total_chars"],
                       not_included=man["not_included"],
                       unconfirmed=[x for n in notes for x in n["coverage"]["unconfirmed"]])
        return {"mode": "complete_in_one_call" if len(parts) == 1 else "multi_part", "part": len(parts), "parts": len(parts),
                "coverage": man, "your_notes_from_earlier_parts": notes, "segments": research_review.part_view(parts[-1])}

    def _completion_gate(self, ver, cumulative):
        if not ver.get("passed"):
            return False, f"verification {ver['status']}"
        if self.cfg["require_changes"] and not any(cumulative.values()):
            return False, "no file changes since task start"
        return True, ""


from taskctl import (dry_run, format_status, latest_task, resolve_approval,  # noqa: E402,F401
                     resolve_task)

def main(*args, **kwargs) -> int:
    """CLI entry point (implemented in cli.py to keep this module small)."""
    from cli import main as _main
    return _main(*args, **kwargs)



if __name__ == "__main__":
    sys.exit(main())
