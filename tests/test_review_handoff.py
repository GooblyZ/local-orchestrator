"""Regression for `review_changes_requested` becoming an owner gate: ordinary Codex corrections go back to Claude
automatically (bounded), repeated/stalled loops END with a blocker report, genuine authorization gates still stop.
Real adapters against SIMULATED CLIs; no model is contacted."""
import unittest
from pathlib import Path

from helpers import ms, orch  # noqa: F401
from test_autonomy import APPROVE, AutoBase, WRITE_GOOD, rv
from test_mvp import PLAN, cx


def fix(i):
    return rv("CONTINUE", criteria=["met", "not_met"], findings=[f"finding {i}: the edge case {i} is not handled"],
              nxt=f"Handle edge case {i} in calc.py and add a test for it.")


def step(i):
    return {"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": f"edge{i}.txt", "content": str(i)}]}


class TestReviewHandoff(AutoBase):
    def completed_task(self, level="balanced"):
        tid, s = self.run_mission([PLAN, APPROVE], [step(0)], level=level)
        self.assertEqual(s["status"], "COMPLETE")
        return tid

    def continue_single(self, tid, codex, claude):
        self.fr.execs[:] = codex
        self.sim.steps[:] = claude
        st, r = self.post("/api/app/continue", {"task_id": tid, "note": "Also handle the edge cases.", "single_pass": True, "confirm": True})
        self.assertEqual(st, 200, r)
        self.wait_idle()
        return self.state(tid)

    def test_corrections_in_a_single_revision_return_to_claude_automatically(self):
        tid = self.completed_task()
        before = self.state(tid)["metrics"]["owner_interventions"]
        s = self.continue_single(tid, [fix(1), fix(2), APPROVE], [step(1), step(2), step(3)])
        self.assertEqual(s["status"], "COMPLETE", s["stop_reason"])
        self.assertEqual(s["metrics"]["owner_interventions"], before, "no owner gate for ordinary review feedback")
        self.assertEqual([d["kind"] for d in s["decisions"]].count("auto_correction"), 2)
        self.assertIn("Handle edge case 1", self.sim.prompts[2], "Codex's requested change was handed to Claude")
        self.assertIn("Handle edge case 2", self.sim.prompts[3])

    def test_bounded_rounds_end_with_a_blocker_report_not_a_question(self):
        tid = self.completed_task()
        s = self.continue_single(tid, [fix(1), fix(2), fix(3)], [step(1), step(2), step(3)])
        self.assertEqual(s["status"], "NEEDS_REVISION", s["stop_reason"])
        self.assertTrue(s["stop_reason"].startswith("unresolved_blocker: corrections_exhausted"), s["stop_reason"])
        self.assertEqual(s["blocker"]["open_findings"], ["finding 3: the edge case 3 is not handled"])
        self.assertEqual(len(self.fr.exec_calls()), 2 + 3, "no model call after the limit")
        self.assertIsNone(s.get("pending_question"))

    def test_repeated_unchanged_findings_end_the_loop(self):
        tid, s = self.run_mission([PLAN, fix(1), fix(1), APPROVE], [step(1), step(2), step(3)])
        self.assertEqual(s["status"], "NEEDS_REVISION", s["stop_reason"])
        self.assertTrue(s["stop_reason"].startswith("unresolved_blocker: repeated_findings"), s["stop_reason"])
        self.assertEqual((len(self.claude_calls()), s["metrics"]["owner_interventions"]), (2, 0))
        self.assertIn("Handle edge case 1", s["blocker"]["next_step"])

    def test_a_supervised_owner_who_chose_a_single_revision_still_decides(self):
        tid = self.completed_task(level="supervised")
        s = self.continue_single(tid, [fix(1)], [step(1)])
        self.assertEqual(s["status"], "PAUSED")
        self.assertTrue(s["stop_reason"].startswith("review_changes_requested"), s["stop_reason"])

    def test_exhausted_review_allowance_is_reported_to_claude_and_an_exhausted_call_budget_pauses_for_the_owner(self):
        req = {"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": "c.md", "content": "concept"}],
               "report": {"summary": "s", "changed_files": ["c.md"], "commands_run": [], "remaining_issues": [],
                          "agent_requests": [{"op": "codex_review", "artifact": "c.md", "focus": "x", "id": "c"}]}}
        tid, s = self.run_mission([PLAN, APPROVE], [req], agent_reviews=0)
        self.assertEqual((s["status"], s["metrics"]["owner_interventions"]), ("COMPLETE", 0))
        self.assertIn("allowance of 0", s["agent_ops"][-1]["reason"])
        tid, s = self.run_mission([PLAN, fix(1), fix(2)], [step(1), step(2)], max_claude_calls=1)
        self.assertEqual(s["status"], "PAUSED")
        self.assertTrue(s["stop_reason"].startswith("budget_reached"), s["stop_reason"])

    def test_a_genuine_new_permission_during_corrections_still_stops_narrowly(self):
        net = {"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": "tools/upload.py", "content": "import urllib.request\n"},
                                           {"op": "capgate", "args": ["script", "tools/upload.py"]}]}
        tid, s = self.run_mission([PLAN, fix(1)], [step(1), net])
        self.assertEqual(s["status"], "HUMAN_REQUIRED")
        self.assertTrue(s["stop_reason"].startswith("capability_approval_required: script tools/upload.py"), s["stop_reason"])
        self.assertEqual([p["suggested_rule"].split("@")[0] for p in s["pending_permissions"]], ["script:tools/upload.py"])

    def test_the_continue_box_no_longer_defaults_to_a_single_revision(self):
        src = (Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('calls: false, single: false})', src)
        self.assertIn("x.checked = C.single === true", src)


if __name__ == "__main__":
    unittest.main()
