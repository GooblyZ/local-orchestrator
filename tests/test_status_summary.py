"""The owner's task status summary ("סיכום מצב המשימה"): AI-authored in the agents' normal replies (Codex after a review,
Claude as a clearly attributed interim), persisted in the task state, never fatal when missing or malformed, never invented,
and generated on request only with explicit confirmation and a counted call. Mock agents / SIMULATED CLIs only."""
import json
import threading
import time
import unittest

from helpers import ms, orch  # noqa: F401
from test_app import OK_REVIEW, WRITE_GOOD, AppBase
from test_goal import GoalBase, bad, cont
from test_long_calls import LongCallBase
from test_mvp import PLAN, cx

import agents  # noqa: E402
import claude_adapter as cla  # noqa: E402
import codex_adapter as ca  # noqa: E402
import web_viewer as wv  # noqa: E402
from app_service import AppService  # noqa: E402
from store import write_control  # noqa: E402

GOOD = ms.GOOD
SUM_CL = {"done": ["כתבתי את add() ב-calc.py"], "checked": ["הרצתי את הבדיקות"], "found": ["טרם אומת: הבדיקות עוברות"],
          "open": ["אין"], "bottom_line": "המימוש נראה תקין, וממתין לביקורת של Codex.", "next": "Codex יבדוק את התוצאה."}
SUM_CX = {"done": ["Claude מימש את add()"], "checked": ["בדקתי את תוצאות הבדיקות שהמערכת הריצה"], "found": ["מאומת: 2 בדיקות עברו"],
          "open": [], "bottom_line": "המשימה הושלמה ואומתה בבדיקות שהמערכת הריצה.", "next": "אין צורך בפעולה נוספת."}


def with_sum(step, summary):
    return dict(step, status_summary_he=summary)


class TestValidation(unittest.TestCase):
    def test_a_malformed_or_non_hebrew_summary_is_ignored_never_fatal(self):
        self.assertEqual(agents.valid_status_summary(SUM_CX)["bottom_line"], SUM_CX["bottom_line"])
        for bad_summary in ({"done": "x"}, {**SUM_CX, "bottom_line": ""}, {k: (["done"] if isinstance(v, list) else "done") for k, v in SUM_CX.items()}, "text"):
            with self.subTest(bad_summary=str(bad_summary)[:40]):
                self.assertIsNone(agents.valid_status_summary(bad_summary))
                rv, ign, _ = agents.validate_review(dict(ms.review("COMPLETE"), status_summary_he=bad_summary), 4000)
                self.assertIsNone(rv["status_summary"])
                self.assertIn("status_summary_he", ign)

    def test_both_agents_are_asked_for_it_in_their_normal_reply(self):
        self.assertIn("status_summary_he", ca.REVIEW_SCHEMA["required"])
        self.assertNotIn("status_summary_he", cla.REPORT_SCHEMA["required"], "optional for Claude: never blocks its report")
        self.assertIn("status_summary_he", cla.REPORT_SCHEMA["properties"])
        for text in (ca.REVIEWER_PROMPT, cla.FRAMING):
            self.assertIn("{summary_rule}", text)
        for needed in ("מאומת:", "טרם אומת:", "השערה:", "Never invent results", "100-200 words"):
            self.assertIn(needed, agents.STATUS_SUMMARY_RULE)


class TestPersistenceAndAttribution(GoalBase):
    def test_completion_keeps_codexs_final_summary_and_claudes_interim_in_history(self):
        o, st = self.goal_run([ms.write_step(GOOD, status_summary_he=SUM_CL)], [with_sum(ms.review("COMPLETE"), SUM_CX)])
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        sm = st["status_summary"]
        self.assertEqual((sm["author"], sm["final"], sm["reviewed_by_codex"], sm["cycle"]), ("codex", True, True, 1))
        self.assertEqual(sm["sections"]["found"], ["מאומת: 2 בדיקות עברו"])
        self.assertEqual([(x["author"], x["bottom_line"]) for x in st["status_summary_history"]], [("claude", SUM_CL["bottom_line"])])

    def test_an_interim_claude_summary_is_attributed_and_a_missing_review_summary_keeps_it(self):
        o, st = self.goal_run([ms.write_step(bad(0), status_summary_he=SUM_CL), ms.write_step(GOOD)],
                              [cont("fix add: return a + b"), with_sum(ms.review("COMPLETE"), {"done": "broken"})])
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        sm = st["status_summary"]
        self.assertEqual((sm["author"], sm["reviewed_by_codex"], sm["cycle"]), ("claude", False, 1),
                         "nothing newer was valid: the latest real summary stays, with its own cycle")

    def test_a_budget_pause_keeps_the_latest_summary(self):
        o, st = self.goal_run([ms.write_step(bad(0)), ms.write_step(GOOD)], [with_sum(cont("fix add: return a + b"), SUM_CX)],
                              {"max_claude_calls": 1})
        self.assertEqual(st["status"], "PAUSED")
        self.assertEqual((st["status_summary"]["author"], st["status_summary"]["cycle"]), ("codex", 1))


class TestCancellationAndCarryOver(LongCallBase):
    def test_a_mid_call_stop_shows_the_last_summary_without_any_extra_model_call(self):
        review = cx(with_sum(ms.review("CONTINUE", findings=["x"], next_instruction="Add a docstring to add() in calc.py.", question=""), SUM_CX))
        o, t = self.run_goal([PLAN, review], [WRITE_GOOD, {"hang": True}], background=True, log=False)
        end = time.time() + 15
        while time.time() < end and len(self.claude_calls()) < 2:
            time.sleep(0.05)
        write_control(o.run_dir, "stop")
        t.join(15)
        st = json.loads((o.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["status"], "STOPPED")
        self.assertEqual((st["status_summary"]["author"], st["status_summary"]["cycle"]), ("codex", 1))
        self.assertEqual(st["calls"], {"codex": 2, "claude": 2}, "no additional call was made to produce a summary")

    def test_codex_updates_the_previous_summary_rather_than_starting_over(self):
        first = cx(with_sum(ms.review("CONTINUE", findings=["x"], next_instruction="Add a docstring to add() in calc.py.", question=""), SUM_CX))
        second = cx(dict(ms.review("COMPLETE", findings=["ok"], next_instruction="", question=""), status_summary_he=SUM_CX))
        o, st = self.run_goal([PLAN, first, second], [WRITE_GOOD, {"ops": [{"op": "write", "path": "notes.md", "content": "n"}]}])
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        prompt2 = self.fr.exec_calls()[2]["kw"]["stdin_text"]
        self.assertIn('"previous_status_summary"', prompt2)
        self.assertIn(SUM_CX["bottom_line"], prompt2)
        self.assertIn("status_summary_he: the Project Owner's status summary of the WHOLE task", prompt2)


class TestBrowserAndOnDemand(AppBase):
    def test_it_survives_a_server_restart_and_is_offered_in_the_browser(self):
        self.fr.execs[:] = [PLAN, cx(with_sum(ms.review("COMPLETE", findings=["ok"], next_instruction="", question=""), SUM_CX))]
        self.sim.steps[:] = [WRITE_GOOD]
        _, tid = self.start_task()
        self.wait_idle()
        self.srv.stop()  # a restarted application reads the saved task
        self.svc = AppService(str(self.cfg_path), agent_factory=self.agents, workspaces_dir=self.wsdir, open_folder=self.opened.append)
        self.srv = wv.ViewerServer(self.runs, 0, app=self.svc).start()
        self.addCleanup(self.srv.stop)
        st = self.get(f"/api/snapshot?task={tid}")[1]["status"]
        self.assertEqual((st["status_summary"]["author"], st["status_summary"]["final"]), ("codex", True))
        self.assertEqual(st["status_summary"]["sections"]["bottom_line"], SUM_CX["bottom_line"])
        self.assertTrue(st["summary_api"])
        html = (orch.Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for needed in ("סיכום מצב המשימה", "השורה התחתונה", "מה הצעד הבא?", "Claude · סיכום ביניים — טרם נבדק ע״י Codex",
                       "פעילות מאז הסיכום (נתוני מערכת — לא סיכום של סוכן)", "עדיין לא נשמר סיכום שנכתב ע״י סוכן",
                       "צור סיכום מהקבצים השמורים (קריאה אחת ל-Codex)", "נספרת בתקציב Codex"):
            self.assertIn(needed, html)

    def test_an_old_task_without_a_summary_gets_one_only_with_confirmation_and_a_counted_call(self):
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        self.sim.steps[:] = [WRITE_GOOD]
        _, tid = self.start_task()
        self.wait_idle()
        self.assertIsNone(self.state(tid).get("status_summary"), "nothing is invented for a task whose agents wrote none")
        before = self.state(tid)["calls"]["codex"]
        self.assertEqual(self.post("/api/app/summarize", {"task_id": tid})[0], 400, "explicit confirmation is required")
        self.assertEqual(self.fr.exec_calls().__len__(), 2)
        self.fr.execs.append(cx({"status_summary_he": SUM_CX}))
        self.assertEqual(self.post("/api/app/summarize", {"task_id": tid, "confirm": True})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual(s["calls"]["codex"], before + 1, "the summary call is counted")
        self.assertEqual((s["status_summary"]["author"], s["status_summary"]["on_request"]), ("codex", True))
        self.assertIn("The Project Owner asked for a status summary", self.fr.exec_calls()[-1]["kw"]["stdin_text"])
        self.assertEqual(s["status"], "COMPLETE", "the task's status and work are unchanged")

    def test_on_demand_summary_respects_the_codex_budget(self):
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        self.sim.steps[:] = [WRITE_GOOD]
        _, tid = self.start_task(settings={"mode": "goal", "kind": "code", "max_codex_calls": 2, "max_claude_calls": 5})
        self.wait_idle()
        st, r = self.post("/api/app/summarize", {"task_id": tid, "confirm": True})
        self.assertEqual(st, 409)
        self.assertIn("codex_budget_exhausted", r["error"])


class TestPageScript(unittest.TestCase):
    def test_the_browser_page_script_is_valid_javascript(self):
        """The app serves web/index.html from disk on every request: a syntax error would break the owner's live view."""
        import re
        import shutil
        import subprocess
        import tempfile
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        html = (orch.Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        js = max(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S), key=len)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
        self.addCleanup(lambda: orch.Path(f.name).unlink(missing_ok=True))
        r = subprocess.run([node, "--check", f.name], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
