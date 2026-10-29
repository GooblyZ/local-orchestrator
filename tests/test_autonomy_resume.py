"""Follow-up: a LEGACY task (created before autonomy levels) resumed under a delegating policy, owner policy changes on a
saved task, model availability without asking the owner to inspect CLI menus, and safe escalation of the product's own
AI-CLI runs. Real adapters + capability gateway against SIMULATED CLIs; no model is contacted."""
import json
import unittest

from helpers import ms, orch  # noqa: F401
from test_autonomy import APPROVE, AutoBase, WRITE_GOOD, rv
from test_mvp import PLAN, cx  # noqa: F401

import autonomy  # noqa: E402

TECH_Q = rv("HUMAN_REQUIRED", question="Should Claude refactor the helper before adding the tests?")
MILO_Q = rv("HUMAN_REQUIRED", question="Which model ID is available in your Codex /model, and do you approve ONE Codex call with "
            "python -I tools/ai_review_retry.py --model <ID> to review the saved script?",
            od={"category": "external_effect", "action": "one Codex CLI call via tools/ai_review_retry.py", "purpose": "real review",
                "affected": "a copy of the test database; ChatGPT subscription usage", "risks": "content goes to the model provider",
                "alternatives": "keep the mocked review"})
AI_SCRIPT = 'import subprocess  # starts the Codex CLI in the real product\nCLI = "codex"\nprint("ai review ok")\n'
CONTINUE_AI = rv("CONTINUE", nxt="Run tools/ai_review_retry.py through the gateway with --model gpt-6-luna, then finish add().")


class LegacyBase(AutoBase):
    def legacy_task(self, codex, claude):
        """A task exactly as created before this upgrade: no 'autonomy' in its state."""
        from goal_mode import make_goal
        self.fr.execs[:] = codex
        self.sim.steps[:] = claude
        cfg = self.svc.cfg(3)
        ws = ms.make_workspace(self.base / "legacy-ws")
        goal = make_goal("goal", "code", {})
        cfg["max_codex_calls"], cfg["max_claude_calls"] = goal["budget"]["max_codex_calls"], goal["budget"]["max_claude_calls"]
        o = orch.Orchestrator(cfg, ws, self.runs, *self.agents(cfg, ws), out=lambda m: None, local_execution=True, goal=goal)
        o.start("legacy Milo-like development task")
        return o.task_id


class TestLegacyResume(LegacyBase):
    def test_a_legacy_task_stays_supervised_until_the_owner_chooses_and_then_codex_decides_without_redoing_work(self):
        tid = self.legacy_task([PLAN, TECH_Q], [{"ops": WRITE_GOOD["ops"]}])
        s = self.state(tid)
        self.assertIsNone(s.get("autonomy"))
        self.assertEqual(s["status"], "HUMAN_REQUIRED")
        self.assertIn("legacy task", s["pending_decision"]["why_owner"], "the owner is told WHY it asked")
        self.assertEqual(self.get(f"/api/snapshot?task={tid}")[1]["status"]["autonomy"]["level"], "legacy")
        before = {k: s[k] for k in ("task_id", "workspace", "budget", "plan", "acceptance_criteria", "approved_tools")}
        calls_before, hist_before = dict(s["calls"]), list(s["history"])
        # the owner chooses 'balanced' for THIS task (no new task) and resumes
        self.assertEqual(self.post("/api/app/autonomy", {"task_id": tid, "level": "balanced"})[0], 200)
        self.fr.execs[:] = [rv("CONTINUE", nxt="Refactor the helper first, then add the tests; run them through the gateway."), APPROVE]
        self.sim.steps[:] = [{"ops": WRITE_GOOD["ops"]}]
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual(s["status"], "COMPLETE", s["stop_reason"])
        self.assertEqual({k: s[k] for k in before}, before, "history, binding, budget, plan, approvals preserved")
        self.assertEqual(s["history"][:len(hist_before)], hist_before)
        self.assertEqual([h["executor"] for h in s["history"]][len(hist_before)], "review-only",
                         "Codex re-decided first; Claude did not repeat its previous step")
        self.assertEqual(len(self.claude_calls()), 2)
        self.assertEqual(s["calls"]["codex"], calls_before["codex"] + 2, "counters continue, never reset")
        self.assertIn("MISSION AUTONOMY POLICY", self.fr.exec_calls()[2]["kw"]["stdin_text"])

    def test_owner_policy_changes_are_validated_logged_and_never_from_agents(self):
        tid = self.legacy_task([PLAN, TECH_Q], [{"ops": WRITE_GOOD["ops"]}])
        for bad in ({"level": "god-mode"}, {"level": "balanced", "fallback_codex": ["gpt-9-imaginary"]},
                    {"level": "balanced", "script_ai_runs": 99}, {"level": "balanced", "script_ai_runs": "3"},
                    {"level": "balanced", "use_model": {"agent": "codex", "model": "gpt-9-imaginary"}}):
            with self.subTest(bad=bad):
                self.assertEqual(self.post("/api/app/autonomy", dict(bad, task_id=tid))[0], 400)
        self.assertIsNone(self.state(tid).get("autonomy"), "a refused change changes nothing")
        ok = {"task_id": tid, "level": "autonomous", "fallback_codex": ["gpt-6-luna"], "script_ai_runs": 2}
        self.assertEqual(self.post("/api/app/autonomy", ok)[0], 200)
        a = self.state(tid)["autonomy"]
        self.assertEqual((a["level"], a["fallback_models"]["codex"], a["script_ai_runs"]), ("autonomous", ["gpt-6-luna"], 2))
        tr = (self.runs / tid / "transcript.jsonl").read_text(encoding="utf-8")
        self.assertIn("AUTONOMY set by the owner", tr)
        self.assertEqual(self.post("/api/app/autonomy", {"task_id": tid, "level": "balanced"}, headers={"X-Orch-CSRF": "x"})[0], 403)


class TestModelAvailability(LegacyBase):
    UNAVAILABLE = {"events": [{"type": "error", "message": "The model gpt-6-sol is not supported when using Codex with a ChatGPT account"}], "rc": 1}

    def test_no_authorized_fallback_pauses_with_the_offered_models_and_one_owner_choice_resumes(self):
        tid, s = self.run_mission([self.UNAVAILABLE], [])
        self.assertEqual(s["status"], "HUMAN_REQUIRED", "a pause with a request, not a dead end")
        self.assertTrue(s["stop_reason"].startswith("model_authorization_required"), s["stop_reason"])
        pd = s["pending_decision"]
        self.assertEqual((pd["category"], pd["agent"]), ("model_authorization", "codex"))
        self.assertIn("gpt-6-luna", pd["offered"], "offered models come from the CLI catalog (no model call)")
        self.assertNotIn("/model", pd["question"])
        self.assertEqual(len(self.fr.exec_calls()), 1, "the failing model is not called again")
        self.assertEqual(self.post("/api/app/autonomy", {"task_id": tid, "use_model": {"agent": "codex", "model": "gpt-9-imaginary"}})[0], 400)
        self.fr.execs[:] = [PLAN, APPROVE]
        self.sim.steps[:] = [{"ops": WRITE_GOOD["ops"]}]
        self.assertEqual(self.post("/api/app/autonomy", {"task_id": tid, "use_model": {"agent": "codex", "model": "gpt-6-luna"}})[0], 200)
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual(s["status"], "COMPLETE", s["stop_reason"])
        models = [c["argv"][c["argv"].index("-m") + 1] for c in self.fr.exec_calls()]
        self.assertEqual(models, ["gpt-6-sol", "gpt-6-luna", "gpt-6-luna"])
        self.assertEqual(s["agent_settings"]["codex_model"], "gpt-6-luna")

    def test_a_pre_authorized_fallback_needs_no_owner_and_no_menu(self):
        tid, s = self.run_mission([self.UNAVAILABLE, PLAN, APPROVE], [{"ops": WRITE_GOOD["ops"]}], fallback_codex=["gpt-6-luna"])
        self.assertEqual((s["status"], self.owner(s)), ("COMPLETE", 0), s["stop_reason"])


class TestAiScriptEscalation(LegacyBase):
    def ai_ops(self, *models):
        return [{"op": "write", "path": "tools/ai_review_retry.py", "content": AI_SCRIPT}] + [
            {"op": "capgate", "args": ["script", "tools/ai_review_retry.py", f"--arg=--model={m}"]} for m in models]

    def test_without_an_allowance_the_owner_gets_the_specific_missing_authorization(self):
        tid, s = self.run_mission([PLAN, MILO_Q], [{"ops": WRITE_GOOD["ops"]}])
        self.assertEqual(s["status"], "HUMAN_REQUIRED")
        self.assertEqual(s["pending_decision"]["why_owner"], autonomy.AI_AUTH_WHY)
        self.assertEqual(len(self.fr.exec_calls()), 2, "not re-asked: running AI features needs the owner's allowance")

    def test_with_an_allowance_codex_decides_but_an_undeclared_ai_script_still_needs_the_owner(self):
        # an AI allowance alone never makes an arbitrary script trusted: only scripts the owner DECLARED at mission setup run
        # (behind the metering shim - see test_agent_operations); the model list reaches Codex without any model call
        claude = [{"ops": WRITE_GOOD["ops"]}, {"ops": self.ai_ops("gpt-6-luna")}]
        tid, s = self.run_mission([PLAN, MILO_Q, CONTINUE_AI, APPROVE], claude, fallback_codex=["gpt-6-luna"], script_ai_runs=1)
        reask = self.fr.exec_calls()[2]["kw"]["stdin_text"]
        self.assertIn("Offered by the installed Codex CLI catalog", reask)
        self.assertIn("gpt-6-luna", reask)
        self.assertIn("Never ask the owner to look up models", reask)
        self.assertEqual([g["rc"] for g in self.sim.gate_runs], [3])
        a = [json.loads(x) for x in (self.runs / tid / "capabilities.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(x["decision"], bool(x.get("ai_cli_run"))) for x in a], [("needs_owner", False)])
        self.assertEqual(s["status"], "HUMAN_REQUIRED")
        self.assertTrue(s["stop_reason"].startswith("capability_approval_required"))

    def test_hard_owner_matters_are_never_rerouted_even_with_an_allowance(self):
        publish = rv("HUMAN_REQUIRED", question="Use the Codex CLI to write a caption and publish the reel to Instagram?",
                     od={"category": "external_effect", "action": "publish", "purpose": "", "affected": "", "risks": "", "alternatives": ""})
        tid, s = self.run_mission([PLAN, publish], [{"ops": WRITE_GOOD["ops"]}], level="autonomous", script_ai_runs=5)
        self.assertEqual(s["status"], "HUMAN_REQUIRED")
        self.assertEqual(len(self.fr.exec_calls()), 2)

    def test_routing_is_decided_by_the_policy_function_not_by_prompt_text(self):
        bal = dict(autonomy.policy({"autonomy": autonomy.make_autonomy("balanced")}), ai_runs_left=0)
        q = lambda text, cat="none": {"verdict": "HUMAN_REQUIRED", "question": text, "owner_decision": {"category": cat}}
        self.assertEqual(autonomy.review_route(q("Which model should the review step use?"), bal, 0)[0], "reask")
        self.assertEqual(autonomy.review_route(q("Which model ID is available for one Codex call?", "external_effect"), bal, 0),
                         ("owner", autonomy.AI_AUTH_WHY))
        self.assertEqual(autonomy.review_route(q("One Codex call via the ai script?", "external_effect"), dict(bal, ai_runs_left=1), 0)[0], "reask")
        self.assertEqual(autonomy.review_route(q("One Codex call, then pay for the paid API tier?", "spending"), dict(bal, ai_runs_left=1), 0)[0], "owner")
        self.assertEqual(autonomy.review_route(q("Which model should we use?"), bal, 1)[0], "owner", "re-asked once only")
        legacy = autonomy.policy({})
        self.assertEqual(autonomy.review_route(q("Which model should we use?"), legacy, 0)[0], "owner")


if __name__ == "__main__":
    unittest.main()
