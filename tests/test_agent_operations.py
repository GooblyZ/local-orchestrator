"""Typed first-party agent operations and the mission-start capability contract, end to end through the browser app, the
real adapters, the capability gateway and the AI-CLI shim (really executed) against SIMULATED CLIs and a FAKE Codex
executable. No model is contacted."""
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from helpers import ms, orch  # noqa: F401
from test_autonomy import APPROVE, AutoBase, WRITE_GOOD, rv
from test_mvp import cx

PLAN_REVIEWS = cx(dict(ms.plan("Write concept.md (the reel concept) and make the tests pass; get Codex's review of the concept."),
                       capabilities_needed=[{"kind": "artifact_reviews", "path": "", "reason": "concept review", "count": 2}]))
ART_FIX = cx({"verdict": "material_corrections", "findings": ["the punchline is missing"],
              "corrections": "Add a clear punchline in the last line of concept.md where Mico reacts.", "user_summary_he": "חסר פאנץ'."})
ART_OK = cx({"verdict": "approved", "findings": ["concept complete"], "corrections": "", "user_summary_he": "מאושר."})
REVIEW_FIX = rv("CONTINUE", criteria=["met", "not_met"], nxt="Apply the corrections from Codex's concept review, then request one more review.")
APPROVE2 = rv("COMPLETE", criteria=["met", "met"], findings=["concept approved by the artifact review; tests pass"])


def report(summary, requests=(), changed=()):
    return {"summary": summary, "changed_files": list(changed), "commands_run": [], "remaining_issues": [],
            "agent_requests": [dict({"op": "codex_review", "focus": "story and punchline"}, **r) for r in requests]}


class TestCollaborationLoop(AutoBase):
    def test_generate_review_correct_rereview_complete_with_zero_owner_prompts(self):
        c1 = {"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": "concept.md", "content": "Milo panics about a parcel."}],
              "report": report("draft concept written", [{"artifact": "concept.md", "id": "concept-v1"}], ["concept.md", "calc.py"])}
        c2 = {"ops": [{"op": "write", "path": "concept.md", "content": "Milo panics about a parcel.\nMico: it's pizza."},
                      {"op": "capgate", "args": ["results", "--id", "concept-v1"]}],
              "report": report("punchline added", [{"artifact": "concept.md", "id": "concept-v2"}], ["concept.md"])}
        tid, s = self.run_mission([PLAN_REVIEWS, ART_FIX, REVIEW_FIX, ART_OK, APPROVE2], [c1, c2])
        self.assertEqual((s["status"], self.owner(s)), ("COMPLETE", 0), s["stop_reason"])
        self.assertEqual([(o["id"], o["status"], o.get("verdict")) for o in s["agent_ops"]],
                         [("concept-v1", "done", "material_corrections"), ("concept-v2", "done", "approved")])
        execs = [c["kw"]["stdin_text"] for c in self.fr.exec_calls()]
        self.assertIn("Milo panics about a parcel.", execs[1], "Codex reviewed the real saved artifact")
        self.assertIn('"agent_operations"', execs[2], "the cycle review sees the operation's result")
        self.assertIn("RESULTS OF YOUR AGENT REQUESTS", self.sim.prompts[1])
        self.assertIn("Add a clear punchline", self.sim.prompts[1], "material findings reach Claude automatically")
        self.assertIn("material_corrections", self.sim.gate_runs[0]["out"], "earlier results are retrievable read-only")
        self.assertEqual(s["metrics"]["agent_reviews_used"], 2)
        self.assertEqual(s["calls"]["codex"], 5, "every operation is a counted Codex call")
        self.assertEqual([d["kind"] for d in s["decisions"]].count("agent_operation"), 2)

    def test_scope_and_allowance_are_enforced_in_code(self):
        (self.base / "secret.md").write_text("outside", encoding="utf-8")
        c1 = {"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": "a.md", "content": "A"}, {"op": "write", "path": "b.md", "content": "B"},
                                          {"op": "write", "path": ".env", "content": "TOKEN=x"}],
              "report": report("x", [{"artifact": "a.md", "id": "a"}, {"artifact": "b.md", "id": "b"}, {"artifact": "../secret.md", "id": "out"}],
                               ["a.md", "b.md", "calc.py"])}
        tid, s = self.run_mission([cx(ms.plan()), ART_OK, APPROVE2], [c1], agent_reviews=1)
        ops = {o["id"]: o for o in self.state(tid).get("agent_ops") or []}
        if s["status"] == "HUMAN_REQUIRED":  # writing .env is a protected change: the cycle stops BEFORE any operation
            self.assertIn("protected path changed: .env", s["stop_reason"])
            self.assertEqual(ops, {})
            return
        self.fail("a protected write must stop the task")

    def test_allowance_and_paths(self):
        c1 = {"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": "a.md", "content": "A"}, {"op": "write", "path": "b.md", "content": "B"}],
              "report": report("x", [{"artifact": "a.md", "id": "a"}, {"artifact": "b.md", "id": "b"}, {"artifact": "../secret.md", "id": "out"}],
                               ["a.md", "b.md", "calc.py"])}
        tid, s = self.run_mission([cx(ms.plan()), ART_OK, APPROVE2], [c1], agent_reviews=1)
        self.assertEqual((s["status"], self.owner(s)), ("COMPLETE", 0), s["stop_reason"])
        ops = {o["id"]: o for o in s["agent_ops"]}
        self.assertEqual(ops["a"]["status"], "done")
        self.assertIn("allowance of 1", ops["b"]["reason"])
        self.assertIn("not allowed", ops["out"]["reason"])
        self.assertEqual(s["calls"]["codex"], 3, "denied operations cost nothing")


FAKE_CODEX_LOG = "fake_codex_calls.txt"
DECLARED = """import shutil, subprocess, sys
exe = shutil.which("codex")  # like the real product: the Codex CLI found on PATH
first = subprocess.run([exe, "exec", "-"], input="review this", capture_output=True, text=True)
second = subprocess.run([exe, "exec", "-"], input="again", capture_output=True, text=True)
bad = subprocess.run([exe, "-m", "gpt-9-imaginary", "exec", "-"], input="x", capture_output=True, text=True)
info = subprocess.run([exe, "features", "list"], capture_output=True, text=True)
print("RESULTS", first.returncode, second.returncode, bad.returncode, info.returncode)
"""


class TestDeclaredProductAiScripts(AutoBase):
    def setUp(self):
        super().setUp()
        self.realbin = self.base / "realbin"
        self.realbin.mkdir()
        self.calls_log = self.base / FAKE_CODEX_LOG
        if os.name == "nt":
            (self.realbin / "codex.cmd").write_text(f'@echo %* >> "{self.calls_log}"\r\n@echo REAL-CODEX-OK\r\n', encoding="utf-8")
        else:
            f = self.realbin / "codex"
            f.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls_log}"\necho REAL-CODEX-OK\n', encoding="utf-8")
            f.chmod(0o755)
        p = mock.patch.dict(os.environ, {"PATH": str(self.realbin) + os.pathsep + os.environ.get("PATH", "")})
        p.start()
        self.addCleanup(p.stop)

    def test_the_milo_interruption_needs_one_authorization_before_work_and_then_none(self):
        plan = cx(dict(ms.plan("Retry only the failed product review through tools/ai_review_retry.py, then make the tests pass."),
                       capabilities_needed=[{"kind": "ai_integration_script", "path": "tools/ai_review_retry.py",
                                             "reason": "real Codex review of the saved script by the product", "count": 1}]))
        run = {"op": "capgate", "args": ["script", "tools/ai_review_retry.py"]}
        claude = [{"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": "tools/ai_review_retry.py", "content": DECLARED}, run]}]
        tid, s = self.run_mission([plan, APPROVE2], claude)
        self.assertEqual(s["status"], "HUMAN_REQUIRED")
        self.assertTrue(s["stop_reason"].startswith("mission_capabilities_required"), s["stop_reason"])
        self.assertEqual(self.claude_calls(), [], "asked BEFORE any work, once, for everything the plan predicts")
        self.assertEqual(s["pending_decision"]["needed"][0]["path"], "tools/ai_review_retry.py")
        grant = {"task_id": tid, "level": "balanced", "ai_scripts": ["tools/ai_review_retry.py"], "script_ai_runs": 1}
        self.assertEqual(self.post("/api/app/autonomy", grant)[0], 200)
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual((s["status"], self.owner(s)), ("COMPLETE", 1), s["stop_reason"])  # the single, up-front authorization
        out = self.sim.gate_runs[0]["out"]
        self.assertIn("RESULTS 0 3 3 0", out, "1st call metered and run; 2nd over the allowance; bad model refused; info passes")
        real = self.calls_log.read_text(encoding="utf-8")
        self.assertEqual(real.count("exec"), 1, "exactly ONE real inference call reached the CLI")
        self.assertIn("-m gpt-6-sol exec", real, "the authorized model was set by the shim")
        a = [json.loads(x) for x in (self.runs / tid / "capabilities.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(x["capability"], x["decision"]) for x in a if x["capability"] == "ai_cli"],
                         [("ai_cli", "allowed"), ("ai_cli", "refused"), ("ai_cli", "refused")])
        self.assertTrue(next(x for x in a if x["capability"] == "script")["declared"])

    def test_declared_scripts_with_network_or_dangerous_commands_and_undeclared_ones_still_stop(self):
        for name, content, declared in (
                ("network", "import socket\nimport subprocess\n", True),
                ("git push", 'import subprocess\nsubprocess.run(["git", "push", "origin", "main"])\n', True),
                ("undeclared", DECLARED, False)):
            with self.subTest(name=name):
                self.sim.gate_runs.clear()
                run = {"op": "capgate", "args": ["script", "tools/x.py"]}
                tid, s = self.run_mission([cx(ms.plan()), APPROVE2],
                                          [{"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": "tools/x.py", "content": content}, run]}],
                                          ai_scripts=["tools/x.py"] if declared else [], script_ai_runs=3)
                self.assertEqual(s["status"], "HUMAN_REQUIRED", name)
                self.assertTrue(s["stop_reason"].startswith("capability_approval_required"), s["stop_reason"])
                self.assertEqual(self.sim.gate_runs[0]["rc"], 3)
                self.assertFalse(self.calls_log.exists() and "exec" in self.calls_log.read_text(encoding="utf-8"),
                                 "no real AI call was made")
                self.post("/api/app/control", {"task_id": tid, "action": "stop"})

    def test_approving_an_ai_script_once_declares_it_and_its_calls_stay_metered(self):
        run = {"op": "capgate", "args": ["script", "tools/ai_review_retry.py"]}
        claude = [{"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": "tools/ai_review_retry.py", "content": DECLARED}, run]},
                  {"ops": [{"op": "write", "path": "tools/ai_review_retry.py", "content": DECLARED + "# edited\n"}, run]}]
        tid, s = self.run_mission([cx(ms.plan()), APPROVE2], claude, script_ai_runs=1)
        self.assertTrue(s["stop_reason"].startswith("capability_approval_required"), s["stop_reason"])
        rule = s["pending_permissions"][0]["suggested_rule"]
        self.assertEqual(self.post("/api/app/permission", {"task_id": tid, "decision": "approve", "rules": [rule]})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual((s["status"], self.owner(s)), ("COMPLETE", 1), s["stop_reason"])
        self.assertEqual(s["autonomy"]["ai_scripts"], ["tools/ai_review_retry.py"], "declared, not a one-off hash approval")
        self.assertIn("RESULTS 0 3 3 0", self.sim.gate_runs[1]["out"], "the EDITED script ran without a new prompt, metered")
        self.assertEqual(self.calls_log.read_text(encoding="utf-8").count("exec"), 1)

    def test_an_approved_network_ai_script_reruns_without_a_second_prompt_and_an_edit_needs_a_new_one(self):
        # round 26/27 of the Milo task: the owner approved a script that calls the AI CLIs AND uses (loopback) networking
        net_ai = DECLARED + "import urllib.request  # talks to the product's own local server\n"
        run = {"op": "capgate", "args": ["script", "tools/live_acceptance.py"]}
        claude = [{"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": "tools/live_acceptance.py", "content": net_ai}, run]},
                  {"ops": [run]},
                  {"ops": [{"op": "write", "path": "tools/live_acceptance.py", "content": net_ai + "# edited\n"}, run]}]
        again = rv("CONTINUE", criteria=["met", "not_met"], nxt="Extend the live acceptance check and run it again.")
        tid, s = self.run_mission([cx(ms.plan()), again], claude, script_ai_runs=2)
        self.assertTrue(s["stop_reason"].startswith("capability_approval_required"), s["stop_reason"])
        rule = s["pending_permissions"][0]["suggested_rule"]
        self.assertEqual(self.post("/api/app/permission", {"task_id": tid, "decision": "approve", "rules": [rule]})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual(s["approved_scripts"], [{"path": "tools/live_acceptance.py", "sha256": rule.rsplit("@", 1)[1]}],
                         "the exact approved version is recorded (not only a declaration)")
        self.assertEqual([g["rc"] for g in self.sim.gate_runs], [3, 0, 3],
                         "1st: asks; the SAME version runs with no second prompt; an EDITED networked version asks again")
        self.assertIn("RESULTS 0 0 3 0", self.sim.gate_runs[1]["out"], "its AI calls stay metered (2 allowed) through the shim")
        self.assertTrue(s["stop_reason"].startswith("capability_approval_required"), "the edited version is a genuinely new request")

    def test_an_approval_lost_from_the_state_is_restored_from_the_task_s_own_decision_log(self):
        net_ai = DECLARED + "import urllib.request\n"
        run = {"op": "capgate", "args": ["script", "tools/live_acceptance.py"]}
        tid, s = self.run_mission([cx(ms.plan())], [{"ops": WRITE_GOOD["ops"] + [
            {"op": "write", "path": "tools/live_acceptance.py", "content": net_ai}, run]}], script_ai_runs=2)
        rule = s["pending_permissions"][0]["suggested_rule"]
        self.fr.execs[:] = [APPROVE2]
        self.sim.steps[:] = [{"ops": [run]}]
        self.assertEqual(self.post("/api/app/permission", {"task_id": tid, "decision": "approve", "rules": [rule]})[0], 200)
        self.wait_idle()
        # simulate the pre-fix record: the decision is in the log, but the state lost the exact-version approval
        st = self.state(tid)
        st["approved_scripts"], st["status"], st["stop_reason"] = None, "PAUSED", "user_pause"
        (self.runs / tid / "state.json").write_text(json.dumps(st), encoding="utf-8")
        self.fr.execs[:] = [APPROVE2]
        self.sim.steps[:] = [{"ops": [run]}]
        self.sim.gate_runs.clear()
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual(self.sim.gate_runs[0]["rc"], 0, "honoured without asking again")
        self.assertEqual(s["approved_scripts"], [{"path": "tools/live_acceptance.py", "sha256": rule.rsplit("@", 1)[1]}])
        ev = [json.loads(x) for x in (self.runs / tid / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("approval_record_restored", [e["event"] for e in ev])

    def test_mission_setup_validates_declared_scripts(self):
        for bad in (["../outside.py"], ["C:/x.py"], ["tools/x.sh"], ["a.py"] * 11):
            with self.subTest(bad=bad):
                self.assertEqual(self.post("/api/app/prepare", {"task": "t", "settings": {"ai_scripts": bad}})[0], 400)
        st, prep = self.post("/api/app/prepare", {"task": "t", "settings": {"ai_scripts": ["tools/ai_review_retry.py"],
                                                                            "script_ai_runs": 2, "agent_reviews": 4}})
        self.assertEqual(st, 200, prep)
        self.assertEqual((prep["settings"]["ai_scripts"], prep["settings"]["script_ai_runs"], prep["settings"]["agent_reviews"]),
                         (["tools/ai_review_retry.py"], 2, 4))


if __name__ == "__main__":
    unittest.main()
