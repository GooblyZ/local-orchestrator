"""The EXACT saved Milo & Mico task (copied read-only into a temp folder), in the state it was left in: PAUSED with
`review_changes_requested` after a single-pass revision. Resuming must send Codex's ordinary technical feedback back to Claude
automatically, end a repeated-findings loop with a blocker report, and still stop for genuine owner matters (time budget).
Simulated agents only; the real task and the Milo project are only read. Skipped where that saved task does not exist."""
import hashlib
import json
import shutil
import unittest
from pathlib import Path

from helpers import orch  # noqa: F401
from test_autonomy import AutoBase, rv
from test_mvp import cx
from helpers import ms

MILO_TASK = "20261015-174534-848ed4"
REAL_RUN = Path(orch.__file__).resolve().parent / "runs" / MILO_TASK
SAME = rv("CONTINUE", criteria=["unverified"], findings=["the diagnosis still infers 'unchanged' from timestamps measured now"],
          nxt="Prove from saved evidence (hashes in the audit log) that the dependency files did not change between rounds 26-27.")


@unittest.skipUnless((REAL_RUN / "state.json").is_file(), "the saved Milo task is not on this machine")
class TestExactMiloTask(AutoBase):
    def copy_task(self, extra_minutes: int):
        real = json.loads((REAL_RUN / "state.json").read_text(encoding="utf-8"))
        self.real_hash = hashlib.sha256((REAL_RUN / "state.json").read_bytes()).hexdigest()
        work = self.base / "milo-copy"
        (work / "tests").mkdir(parents=True)
        (work / "tests" / "test_ok.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n"
                                                   "        self.assertTrue(True)\n", encoding="utf-8")
        run = self.runs / MILO_TASK
        run.mkdir(parents=True)
        for f in ("transcript.jsonl", "events.jsonl", "baseline.json", "capabilities.jsonl"):
            if (REAL_RUN / f).exists():
                shutil.copy2(REAL_RUN / f, run / f)
        s = dict(real, workspace=str(work))
        s["acceptance_criteria"] = s["acceptance_criteria"][:1]  # Codex reports one criterion in these scripted reviews
        s["budget"] = dict(s["budget"], max_minutes=s["budget"]["max_minutes"] + extra_minutes)  # the owner's extension, if any
        (run / "state.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
        self.sim.sessions[s["executor_session_id"]] = []  # the same Claude session continues
        return s

    def test_resume_returns_review_feedback_to_claude_and_ends_a_repeated_loop_without_asking(self):
        s0 = self.copy_task(extra_minutes=30)
        if not s0["stop_reason"].startswith("review_changes_requested"):
            self.skipTest("the saved task has moved past the round-29 state (covered generically by test_review_handoff)")
        self.fr.execs[:] = [SAME, SAME]
        self.sim.steps[:] = [{"ops": [{"op": "write", "path": "notes_r30.md", "content": "attempt 1"}]},
                             {"ops": [{"op": "write", "path": "notes_r31.md", "content": "attempt 2"}]}]
        self.assertEqual(self.post("/api/app/control", {"task_id": MILO_TASK, "action": "resume"})[0], 200)
        self.wait_idle()
        s = self.state(MILO_TASK)
        # Codex's requested change went to Claude (the stored instruction), its next request too - no owner gate
        self.assertIn(s0["instruction"][:80], self.sim.prompts[0])
        self.assertIn("Prove from saved evidence", self.sim.prompts[1], "the review feedback was handed to Claude automatically")
        self.assertEqual(len(self.claude_calls()), 2)
        # identical findings after the correction: the loop ENDS with a blocker report, not a question
        self.assertEqual(s["status"], "NEEDS_REVISION", s["stop_reason"])
        self.assertTrue(s["stop_reason"].startswith("unresolved_blocker: repeated_findings"), s["stop_reason"])
        self.assertEqual(s["blocker"]["open_findings"], ["the diagnosis still infers 'unchanged' from timestamps measured now"])
        self.assertIn("Prove from saved evidence", s["blocker"]["next_step"])
        self.assertEqual(s["metrics"]["owner_interventions"], s0["metrics"]["owner_interventions"], "no new owner prompt")
        ev = [json.loads(x)["event"] for x in (self.runs / MILO_TASK / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        after = ev[len(ev) - 1 - ev[::-1].index("task_resume"):]
        self.assertNotIn("review_changes_requested", after)
        self.assertNotIn("human_required", after)
        self.assertIsNone(s.get("pending_permissions"), "the stale round-27 permission card was cleared on resume")
        # budgets, history, approvals and settings survive
        self.assertEqual(s["calls"], {"claude": s0["calls"]["claude"] + 2, "codex": s0["calls"]["codex"] + 2})
        self.assertEqual(s["history"][:len(s0["history"])], s0["history"])
        self.assertEqual((s["approved_tools"], s.get("approved_scripts"), s["autonomy"], s["agent_settings"]),
                         (s0["approved_tools"], s0.get("approved_scripts"), s0["autonomy"], s0["agent_settings"]))
        self.assertEqual(hashlib.sha256((REAL_RUN / "state.json").read_bytes()).hexdigest(), self.real_hash)


    def test_the_round_26_approval_of_live_acceptance_is_honoured_without_a_second_request(self):
        """Round 26: the owner approved script:tools/live_acceptance.py@6da73b10...; round 27 asked again for the SAME
        version (defect). On the copy, the approval is restored from the task's own decision log and the gateway runs
        that exact version (only `--help`, which exits before starting anything) without asking."""
        real = json.loads((REAL_RUN / "state.json").read_text(encoding="utf-8"))
        milo = Path(real["workspace"])
        approved = [d for d in (json.loads(x) for x in (REAL_RUN / "events.jsonl").read_text(encoding="utf-8").splitlines())
                    if d.get("event") == "permission_decision" and (d.get("data") or {}).get("approve") is True
                    and any("live_acceptance" in r for r in d["data"].get("rules") or [])]
        if not approved or not (milo / "tools" / "live_acceptance.py").is_file():
            self.skipTest("no recorded live_acceptance approval on this machine")
        rule = next(r for r in approved[-1]["data"]["rules"] if "live_acceptance" in r)
        s0 = self.copy_task(extra_minutes=0)
        work = Path(s0["workspace"])
        (work / "tools").mkdir()
        shutil.copy2(milo / "tools" / "live_acceptance.py", work / "tools" / "live_acceptance.py")
        shutil.copytree(milo / "studio", work / "studio", ignore=shutil.ignore_patterns("__pycache__"))
        sha = hashlib.sha256((work / "tools" / "live_acceptance.py").read_bytes()).hexdigest()
        self.assertEqual(rule, f"script:tools/live_acceptance.py@{sha}", "the file on disk is exactly the version approved in round 26")
        if s0.get("approved_scripts"):
            self.skipTest("the real task was already healed by a resume (approval restored); covered by test_agent_operations")
        st = json.loads((self.runs / MILO_TASK / "state.json").read_text(encoding="utf-8"))
        st["budget"] = dict(st["budget"], max_claude_calls=st["calls"]["claude"] + 1)  # one step, then a budget pause ends the test
        (self.runs / MILO_TASK / "state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        self.fr.execs[:] = [cx(dict(ms.plan("Run the live acceptance check through the gateway."), capabilities_needed=[])),
                            rv("CONTINUE", criteria=["unverified"], nxt="Report the live acceptance result.")]
        self.sim.steps[:] = [{"ops": [{"op": "capgate", "args": ["script", "tools/live_acceptance.py", "--arg=--help", "--isolated"]}]}]
        st, r = self.post("/api/app/continue", {"task_id": MILO_TASK, "note": "Run the live acceptance check.", "confirm": True})
        self.assertEqual(st, 200, r)
        self.wait_idle()
        s = self.state(MILO_TASK)
        self.assertEqual(self.sim.gate_runs[0]["rc"], 0, self.sim.gate_runs[0]["out"][-400:])
        self.assertIn("usage:", self.sim.gate_runs[0]["out"])
        audit = [json.loads(x) for x in (self.runs / MILO_TASK / "capabilities.jsonl").read_text(encoding="utf-8").splitlines()]
        last = audit[-1]
        self.assertEqual((last["decision"], (last.get("script") or {}).get("sha256")), ("allowed", sha))
        self.assertIn("approved by the owner", last["reason"])
        self.assertIn({"path": "tools/live_acceptance.py", "sha256": sha}, s["approved_scripts"])
        self.assertFalse(str(s["stop_reason"]).startswith("capability_approval_required"), s["stop_reason"])
        ev = [json.loads(x)["event"] for x in (self.runs / MILO_TASK / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("approval_record_restored", ev)
        self.assertTrue(s["stop_reason"].startswith("budget_reached"), "only the test's one-step budget ended the run")
        self.assertEqual((s["calls"]["claude"], s["budget"]["max_minutes"]), (s0["calls"]["claude"] + 1, s0["budget"]["max_minutes"]))
        self.assertEqual(s["history"][:len(s0["history"])], s0["history"])
        self.assertEqual(hashlib.sha256((REAL_RUN / "state.json").read_bytes()).hexdigest(), self.real_hash)

if __name__ == "__main__":
    unittest.main()
