"""Orchestrator mixin: bounded autonomous recovery, review routing and the capability gateway, driven ONLY by the task's
persisted, owner-authorized autonomy policy (autonomy.py). Nothing here can widen permissions: model output is routed,
retried or escalated, never treated as an authorization.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import autonomy
from agents import AdapterError
from store import atomic_write_json


class AutopilotMixin:
    sleep = staticmethod(time.sleep)  # tests replace it; real backoff waits are short and bounded (autonomy.backoff_seconds)

    # ------------------------------------------------------------------ policy
    def _pol(self) -> dict:
        return autonomy.policy(self.state)

    def _offered_models(self) -> dict:
        """The installed Codex CLI's model catalog, read once without inference (`codex debug models`)."""
        if getattr(self, "_offered", None) is None:
            fn = getattr(self.planner, "available_models", None)
            try:
                self._offered = {"codex": list(fn()) if fn else []}
            except Exception:
                self._offered = {"codex": []}
        return self._offered

    def _rich_pol(self) -> dict:
        """The policy plus the facts the decision layer needs: authorized models, the catalog, AI runs left."""
        pol = self._pol()
        left = max(0, pol.get("script_ai_runs", 0) - autonomy.ai_runs_used(self.run_dir / "capabilities.jsonl"))
        return dict(pol, ai_runs_left=left, models=autonomy.authorized_models(self.state, pol),
                    offered_models=self._offered_models())

    def _decide(self, kind: str, what: str, why: str, **extra) -> None:
        """Record an autonomous decision: state (for the owner's panel), event log and a visible conversation notice."""
        d = autonomy.record(self.state, kind, what, why, **extra)
        self.log.write("auto_decision", self.state.get("cycle"), **{k: v for k, v in d.items() if k != "cycle"})
        self._msg("orchestrator", "user", "notice", f"AUTONOMOUS DECISION ({kind}): {what} - why: {why}", self.state.get("cycle"),
                  auto_decision=kind)

    # ------------------------------------------------------------------ model calls with bounded recovery
    def _call_model(self, agent: str, role: str, c, fn):
        """Run one model call. Transient errors are retried with backoff (each retry is a counted call, only while budget
        remains); an unavailable model falls back ONLY to a model the owner authorized for this task. Every failure is
        classified and logged; anything else is raised unchanged to the existing handling."""
        s, pol, attempt = self.state, self._pol(), 0
        tried = set()
        while True:
            try:
                with self._phase(agent, role, c):
                    out = fn()
                if attempt:
                    autonomy.bump(s, "recoveries_succeeded")
                return out
            except AdapterError as e:
                cls = autonomy.classify_failure(e.kind, e.message)
                e.failure = cls
                self.log.write("failure_classified", c, agent=agent, role=role, kind=e.kind, **cls)
                adapter = self.executor if agent == "claude" else self.planner
                started = agent == "claude" and (getattr(self.executor, "last_meta", None) or {}).get("session_id")
                if (cls["class"] == "transient" and attempt < pol["transient_retries"] and not started
                        and self._remaining(agent) > 0):
                    attempt += 1
                    wait = autonomy.backoff_seconds(attempt)
                    self._decide("recovery", f"retry {agent} ({role}) after a temporary error, attempt {attempt} of "
                                 f"{pol['transient_retries']}, after {wait:.0f}s", f"{cls['diagnosis']} ({e.message[:160]})")
                    self.sleep(wait)
                    self._spend(agent)
                    continue
                if cls["class"] == "model_unavailable" and pol["model_fallback"] and hasattr(adapter, "switch_model"):
                    before = getattr(adapter, "model", "?")
                    nxt = next((m for m in pol["fallback_models"].get(agent) or [] if m not in tried and m != before), None)
                    if nxt and self._remaining(agent) > 0:
                        tried.add(nxt)
                        if adapter.switch_model(nxt):
                            key = "codex_model" if agent == "codex" else "claude_model"
                            s.setdefault("agent_settings", {})[key] = nxt  # a resume keeps using the working model
                            self._decide("model_fallback", f"{agent}: {before} -> {nxt}", "the selected model is unavailable; "
                                         f"{nxt} is a fallback the owner authorized for this task and the CLI offers it")
                            self._spend(agent)
                            continue
                        self.log.write("model_fallback_rejected", c, agent=agent, model=nxt, why="not offered by the installed CLI")
                if cls["class"] == "model_unavailable" and not s.get("pending_decision"):
                    self._model_authorization(agent, adapter, e)
                raise

    def _model_authorization(self, agent: str, adapter, e) -> None:
        """No owner-authorized fallback works: PAUSE (never finish, never guess, never a paid option) and ask for exactly
        the missing authorization, with the models the installed CLI offers (read without a model call)."""
        s = self.state
        offered = self._offered_models().get(agent, []) if agent == "codex" else []
        current = getattr(adapter, "model", "?")
        q = (f"The {agent} model {current} is not available to this subscription/CLI ({e.message[:160]}). Authorize a fallback "
             f"model for this task" + (f" - offered by the installed CLI: {', '.join(m for m in offered if m != current)}" if offered else "")
             + ", then resume. Nothing else was tried; no paid option is used.")
        s["pending_decision"] = {"category": "model_authorization", "agent": agent, "question": q, "offered": offered,
                                 "why_owner": "only the owner authorizes which models a task may use"}
        e.halted = True
        self._halt(f"model_authorization_required: {agent} {current} unavailable", q)

    # ------------------------------------------------------------------ capability gateway
    def _write_cap_policy(self, c) -> str:
        s, pol = self.state, self._pol()
        path = self.run_dir / "capabilities.json"
        shim_dir, shim_pol = self._write_shim(pol)
        atomic_write_json(path, {"version": 1, "task_id": self.task_id, "cycle": c, "root": str(self.workspace),
                                 "ai_scripts": list(pol.get("ai_scripts") or []), "shim_dir": shim_dir, "shim_policy": shim_pol,
                                 "agent_ops": str(self.run_dir / "agent_ops.json"),
                                 "audit": str(self.run_dir / "capabilities.jsonl"), "level": pol["level"],
                                 "scripts": pol["scripts"], "approved_scripts": s.get("approved_scripts") or [],
                                 "ai_runs_allowed": pol.get("script_ai_runs", 0),
                                 "ai_models": [m for ms in autonomy.authorized_models(s, pol).values() for m in ms],
                                 "protected": list(self.cfg["protected_path_patterns"]),
                                 "test_timeout": self.cfg["verification_timeout_sec"], "script_timeout": 600})
        return str(path)

    def _restore_recorded_approvals(self) -> list:
        """The owner's exact-version script approvals as RECORDED in this task's own decision log (permission_decision,
        approve=true). Restores any that the task state lost (a defect before 2026-10-18 recorded some only as a
        declaration). Nothing new is approved: only rules the owner approved, for the same path and SHA-256."""
        import re
        s, rx = self.state, re.compile(r"^script:([A-Za-z0-9_./ -]{1,200}\.py)@([0-9a-f]{64})$")
        p = self.run_dir / "events.jsonl"
        if not p.exists():
            return []
        have = {(x.get("path"), x.get("sha256")) for x in s.get("approved_scripts") or []}
        restored = []
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            d = e.get("data") or {}
            if e.get("event") != "permission_decision" or d.get("approve") is not True:
                continue
            for rule in d.get("rules") or []:
                m = rx.match(str(rule))
                if m and (m.group(1), m.group(2)) not in have:
                    have.add((m.group(1), m.group(2)))
                    restored.append({"path": m.group(1), "sha256": m.group(2)})
        if restored:
            s["approved_scripts"] = (s.get("approved_scripts") or []) + restored
            self.log.write("approval_record_restored", s.get("cycle"), restored=restored,
                           why="owner approvals found in this task's decision log but missing from its state")
        return restored

    def _write_shim(self, pol: dict) -> tuple[str, str]:
        """codex/claude wrappers that route an owner-declared script's AI CLI calls through aishim.py (metered, model-checked)."""
        import os
        import sys
        d = self.run_dir / "shim"
        d.mkdir(exist_ok=True)
        gate = (Path(__file__).resolve().parent / "aishim.py")
        for tool in ("codex", "claude"):
            if os.name == "nt":
                (d / f"{tool}.cmd").write_text(f'@"{sys.executable}" -I "{gate}" {tool} %*\r\n', encoding="utf-8")
            else:
                f = d / tool
                f.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{gate}" {tool} "$@"\n', encoding="utf-8")
                f.chmod(0o755)
        sp = self.run_dir / "ai_shim.json"
        real = os.pathsep.join(x for x in os.environ.get("PATH", "").split(os.pathsep)
                               if x and os.path.normcase(os.path.abspath(x)) != os.path.normcase(str(d)))
        atomic_write_json(sp, {"task_id": self.task_id, "audit": str(self.run_dir / "capabilities.jsonl"),
                               "allowed_runs": pol.get("script_ai_runs", 0), "models": autonomy.authorized_models(self.state, pol),
                               "real_path": real})
        return str(d), str(sp)

    # ------------------------------------------------------------------ typed agent operations
    def _run_agent_ops(self, c, report) -> list:
        """Claude's agent_requests, executed by the ORCHESTRATOR: scope (a readable project file), the task's authorized Codex
        model and budget, and the owner's allowance are enforced here, in code. Results go to Codex's cycle review now and
        to Claude at its next step. A denied request is reported back to Claude, never escalated by itself."""
        import hashlib
        from agents import parse_json_object, validate_artifact_review
        from safety import PolicyViolation
        from store import now
        reqs, out = list(report.get("agent_requests") or []), []
        out += [{"id": "-", "op": "?", "status": "denied", "reason": r["reason"], "cycle": c} for r in report.get("agent_requests_rejected") or []]
        s, pol = self.state, self._pol()
        for r in reqs:
            res = {"id": r["id"], "op": r["op"], "artifact": r["artifact"], "cycle": c, "at": now()}
            used = (s.get("metrics") or {}).get("agent_reviews_used", 0)
            why, p, rel, data = None, None, None, b""
            try:
                p, rel = self.policy.check_path(r["artifact"], "read")
            except PolicyViolation as e:
                why = f"artifact not allowed: {e}"
            if not why and (not p.is_file() or p.is_symlink()):
                why = "artifact not found (save it as a file in the project first)"
            if not why:
                data = p.read_bytes()
                why = ("binary artifact (export it as text first)" if b"\x00" in data[:4096] else
                       "artifact too large (over 150,000 bytes)" if len(data) > 150_000 else None)
            if not why and used >= pol.get("agent_reviews", 0):
                why = f"the owner's allowance of {pol.get('agent_reviews', 0)} Codex artifact reviews for this task is used up"
            if not why and self._remaining("codex") < 2:
                why = "the Codex call budget is reserved for the cycle review"
            if not why and not hasattr(self.reviewer, "review_artifact"):
                why = "this reviewer cannot run artifact reviews"
            if why:
                out.append(dict(res, status="denied", reason=why))
                self.log.write("agent_operation", c, **{k: v for k, v in out[-1].items() if k != "cycle"})
                continue
            res["sha256"] = hashlib.sha256(data).hexdigest()
            ctx = {"task": self._agent_task(), "acceptance_criteria": s.get("acceptance_criteria") or [], "path": rel,
                   "sha256": res["sha256"], "focus": r["focus"], "content": self.redactor.text(data.decode("utf-8", "replace"))}
            v = None
            for attempt in range(2):  # a malformed answer: the REVIEW alone is retried once (counted)
                self._spend("codex")
                try:
                    v = validate_artifact_review(parse_json_object(
                        self._call_model("codex", "artifact_review", c, lambda: self.reviewer.review_artifact(ctx))))
                    break
                except AdapterError as e:
                    if getattr(e, "halted", False):
                        res.update(status="failed", reason="waiting for the owner (model authorization)")
                        out.append(res)
                        self._store_ops(out)
                        return out
                    if e.kind != "malformed" or attempt or self._remaining("codex") < 2:
                        res.update(status="failed", reason=f"{e.kind}: {e.message[:300]}")
                        break
            if v:
                autonomy.bump(s, "agent_reviews_used")
                res.update(status="done", model=getattr(self.reviewer, "model", None), **v)
                self._msg("codex", "claude", "agent_result", json.dumps(res, ensure_ascii=False), c)
                self._decide("agent_operation", f"Codex reviewed {rel} for Claude: {v['verdict']}",
                             f"typed agent operation within the owner's allowance ({used + 1} of {pol.get('agent_reviews', 0)}), "
                             "authorized model, counted in the Codex budget")
            out.append(res)
            self.log.write("agent_operation", c, **{k: res.get(k) for k in ("id", "op", "artifact", "status", "verdict", "reason")})
        self._store_ops(out)
        return out

    def _store_ops(self, out: list) -> None:
        s = self.state
        s["agent_ops"] = ((s.get("agent_ops") or []) + out)[-30:]
        atomic_write_json(self.run_dir / "agent_ops.json", s["agent_ops"])
        self._save()

    def _agent_results_for(self, c) -> list:
        """What Claude gets at its next step: the results of the operations it requested in the previous step."""
        return [o for o in self.state.get("agent_ops") or [] if o.get("cycle") == c - 1][-5:]

    # ------------------------------------------------------------------ mission capabilities
    def _capability_gate(self, needed: list) -> bool:
        """Right after planning, BEFORE any work: capabilities the plan predicts but the owner has not authorized are
        presented together, once. True = the mission may start."""
        pol = self._pol()
        if pol["level"] == "legacy" or not needed:
            return True
        missing = []
        for cap in needed:
            if cap["kind"] == "artifact_reviews" and cap["count"] > pol.get("agent_reviews", 0):
                missing.append(cap)
            elif cap["kind"] == "ai_integration_script" and (cap["path"] not in (pol.get("ai_scripts") or [])
                                                           or cap["count"] > pol.get("script_ai_runs", 0)):
                missing.append(cap)
        if not missing:
            return True
        s = self.state
        s["pending_decision"] = {"category": "mission_capabilities", "needed": missing,
                                 "question": "Before any work starts, the plan needs capabilities this mission does not have yet: "
                                             + "; ".join(f"{m['kind']} {m['path']} x{m['count']} ({m['reason'][:120]})" for m in missing)
                                             + ". Authorize them together (or continue without them).",
                                 "why_owner": "only the owner widens what a mission may do; asked once, before execution"}
        self._halt("mission_capabilities_required: " + ", ".join(f"{m['kind']}:{m['path'] or m['count']}" for m in missing),
                   s["pending_decision"]["question"])
        return False

    def _audit_offset(self) -> int:
        p = self.run_dir / "capabilities.jsonl"
        return p.stat().st_size if p.exists() else 0

    def _cap_records(self, offset: int) -> list[dict]:
        p = self.run_dir / "capabilities.jsonl"
        if not p.exists():
            return []
        with open(p, "rb") as f:
            f.seek(offset)
            lines = f.read().decode("utf-8", "replace").splitlines()
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        if out:
            autonomy.bump(self.state, "capability_runs", sum(1 for r in out if r.get("decision") == "allowed"))
        return out

    @staticmethod
    def _cap_evidence(records: list[dict]) -> list[dict]:
        return [{k: r.get(k) for k in ("capability", "args", "decision", "reason", "exit_code", "runs", "script", "findings")}
                for r in records][-20:]

    def _capability_halt(self, records: list[dict]):
        """A script the gateway would not run without the owner: one decision card per script version."""
        s, seen, pend = self.state, set(), []
        for r in records:
            sc = r.get("script") or {}
            key = (sc.get("path"), sc.get("sha256"))
            if r.get("decision") != "needs_owner" or key in seen:
                continue
            seen.add(key)
            pend.append({"tool": "capability", "kind": "script", "input": f"script {sc.get('path')}",
                         "suggested_rule": f"script:{sc.get('path')}@{sc.get('sha256')}", "findings": r.get("findings") or [],
                         "reason": r.get("reason")})
        meta = getattr(self.executor, "last_meta", None) or {}
        if meta.get("session_id"):
            s["executor_session_id"] = meta["session_id"]
        s["pending_permissions"] = pend
        names = ", ".join(p["input"] for p in pend)
        return self._halt(f"capability_approval_required: {names}",
                          f"Claude needs to run {names}. The gateway found possible external effects "
                          f"({'; '.join(', '.join(p['findings']) for p in pend) or 'supervised mission'}). Approve this exact "
                          "script version for this task only, or deny it (Claude continues without it).")

    # ------------------------------------------------------------------ blocked shell commands
    def _redirectable(self, exc, redirects: int) -> list | None:
        """Denied Bash commands that the gateway covers -> the denials (Claude is redirected without the owner); else None."""
        from claude_adapter import CAPGATE_RULE
        pol = self._pol()
        meta = getattr(self.executor, "last_meta", None) or {}
        denials = meta.get("denials") or []
        if (not isinstance(exc, AdapterError) or exc.kind != "permission" or not denials or not meta.get("session_id")
                or not pol["redirect_blocked_commands"] or redirects >= pol["max_redirects"] or CAPGATE_RULE is None
                or self._remaining("claude") <= 0 or self.state.get("file_spec")):
            return None
        return denials if all(autonomy.classify_denial(d) == "routine" for d in denials) else None

    # ------------------------------------------------------------------ reviews
    def _review_with_policy(self, c, review_ctx):
        """Codex reviews; a TECHNICAL escalation to the owner is sent back to Codex once to decide (counted call).
        Returns (raw, validated review, route, why)."""
        from agents import parse_json_object, validate_review
        s, pol = self.state, self._rich_pol()
        if pol["codex_decides_technical"]:
            review_ctx = dict(review_ctx, autonomy_note=autonomy.reviewer_policy_note(pol))
        reasks = 0
        while True:
            raw = self._call_model("codex", "reviewer", c, lambda: self.reviewer.review(review_ctx))
            try:
                rv, ign, perm = validate_review(parse_json_object(raw), self.cfg["max_instruction_chars"])
            except AdapterError as e:
                e.raw = raw
                raise
            self._note_ignored("reviewer", ign, perm)
            route, why = autonomy.review_route(rv, pol, reasks)
            if route != "reask":
                return raw, rv, route, why
            if self._remaining("codex") <= 0:
                return raw, rv, "owner", "no Codex call left to settle the technical question"
            reasks += 1
            self._msg("codex", "orchestrator", "review", raw, c, superseded_by_policy=True)
            self._decide("policy_reask", "Codex's question was returned to Codex to decide: " + str(rv.get("question"))[:300], why)
            self._spend("codex")
            review_ctx = dict(review_ctx, policy_reask=autonomy.reask_note(rv, pol))

    def _criteria_gate(self, rv) -> tuple[bool, str, bool]:
        """(ok, why, only_unverified): COMPLETE needs every acceptance criterion 'met' when Codex reports them."""
        cs, crit = rv.get("criteria_status"), self.state.get("acceptance_criteria") or []
        if not cs or len(cs) != len(crit):
            return True, "", False
        open_ = [(crit[i], x) for i, x in enumerate(cs) if x != "met"]
        if not open_:
            return True, "", False
        return (False, f"{len(open_)} acceptance criteria are not met or unverified: "
                + "; ".join(f"[{x}] {t[:120]}" for t, x in open_[:5]), all(x == "unverified" for _, x in open_))
