"""Owner continuation of a saved task ("המשך משימה"): nothing is reset or rewritten, the owner's note is a separately labelled
new instruction, Codex plans the continuation from the persisted record, Claude continues in the same session when it still
exists (otherwise with the full task and a brief), budgets continue (extensions only upwards, explicit), one continuation at
a time, safe across restarts. Mock agents / SIMULATED CLIs only."""
import hashlib
import json
import time
import unittest

from helpers import ms, orch  # noqa: F401
from test_app import AppBase, Gate
from test_goal import Base
from test_long_calls import HEBREW_REPORT, MEMORY_TASK, LongCallBase
from test_mvp import PLAN, cx

import app_service  # noqa: E402
import goal_mode as gm  # noqa: E402
import web_viewer as wv  # noqa: E402
from agents import make_mock_agents  # noqa: E402
from config import OrchestratorError  # noqa: E402
from store import atomic_write_json, write_control  # noqa: E402
from taskctl import prepare_continuation, remaining_budget  # noqa: E402

TASK = "# Financial research\nFind a real, repeatable edge. Decision: GO / PIVOT / NO-GO / INCONCLUSIVE."
NOTE = "שלב 2: בדוק גם הזדמנויות שלא נבדקו — אסטרטגיות עונתיות ופערי נזילות בשוקי אופציות."


def report(v, extra=""):
    return HEBREW_REPORT.format(verdict=v) + extra


def write(v, extra=""):
    return {"ops": [{"op": "write", "path": gm.RESEARCH_REPORT, "content": report(v, extra)}], "report": {}}


CONT_PLAN = {"plan": "Phase 2: seasonal and liquidity-gap strategies.",
             "acceptance_criteria": ["The report covers seasonal strategies and option-market liquidity gaps with dated sources."],
             "instruction": "INCONCLUSIVE so far for momentum; now research seasonal strategies and liquidity gaps; update the report."}


class ContinuationBase(Base):
    def first_run(self, planner_steps, steps, reviews, budget=None, task=TASK):
        self.agents = make_mock_agents({"planner": planner_steps, "executor": steps, "reviewer": reviews})
        o = orch.Orchestrator(self.cfg(), self.ws, self.runs, *self.agents, out=self.lines.append,
                              goal=gm.make_goal("goal", "research", budget))
        return o, o.start(task)

    def resume(self, o):
        return orch.Orchestrator(self.cfg(), self.ws, self.runs, *self.agents, out=self.lines.append, task_id=o.task_id).resume()

    def msgs(self, o):
        return [json.loads(x) for x in (o.run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]

    def state(self, o):
        return json.loads(o.state_path.read_text(encoding="utf-8"))


class TestContinuingAnInconclusiveResearchTask(ContinuationBase):
    def test_with_new_instructions_nothing_is_lost_and_the_work_really_continues(self):
        o, st = self.first_run([ms.plan(), CONT_PLAN], [write("INCONCLUSIVE"), write("PIVOT", "\nשלב 2 נבדק.\n")],
                               [ms.review("COMPLETE"), ms.review("COMPLETE")])
        self.assertEqual(st["status"], "INCONCLUSIVE")
        before = {"report": hashlib.sha256((self.ws / gm.RESEARCH_REPORT).read_bytes()).hexdigest(), "elapsed": st["elapsed_sec"],
                  "calls": dict(st["calls"]), "criteria": list(st["acceptance_criteria"]), "history": len(st["history"])}
        entry = prepare_continuation(self.runs, o.task_id, NOTE)
        s = self.state(o)
        self.assertEqual((s["status"], s["task"]), ("PAUSED", TASK), "the original request is never rewritten")
        self.assertEqual(hashlib.sha256((self.ws / gm.RESEARCH_REPORT).read_bytes()).hexdigest(), before["report"], "artifacts untouched")
        self.assertEqual((entry["prior"]["status"], entry["prior"]["verdict"]), ("INCONCLUSIVE", "INCONCLUSIVE"))
        self.assertEqual(s["calls"], before["calls"], "no counter is reset")
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        cont = self.agents[0].calls[1]
        self.assertIn(NOTE, cont["task"])
        self.assertIn("OWNER'S CONTINUATION #1", cont["task"])
        self.assertTrue(cont["task"].startswith(TASK), "original first, the note after it, clearly labelled")
        self.assertEqual((cont["continuation"]["prior"]["verdict"], cont["continuation"]["note"]), ("INCONCLUSIVE", NOTE))
        self.assertEqual(cont["continuation"]["previous_acceptance_criteria"], before["criteria"])
        self.assertEqual(st2["acceptance_criteria"], before["criteria"] + CONT_PLAN["acceptance_criteria"], "original criteria kept, new added")
        ex = self.agents[1].calls[-1]
        self.assertEqual(ex["instruction"], CONT_PLAN["instruction"])
        self.assertEqual(ex["owner_notes"][0]["note"], NOTE)
        self.assertEqual(ex["continuation_brief"]["prior"]["verdict"], "INCONCLUSIVE")
        self.assertEqual(len(st2["history"]), before["history"] + 1, "history is appended, never replaced")
        self.assertGreaterEqual(st2["elapsed_sec"], before["elapsed"])
        self.assertEqual(st2["calls"], {"codex": before["calls"]["codex"] + 2, "claude": before["calls"]["claude"] + 1})
        kinds = [m["kind"] for m in self.msgs(o)]
        self.assertEqual(kinds.count("final"), 2, "the previous INCONCLUSIVE outcome stays in the conversation")
        self.assertLess(kinds.index("final"), kinds.index("continuation"))
        self.assertIn("continuation #1", [m["content"] for m in self.msgs(o) if m["kind"] == "notice"][-1])

    def test_without_instructions_the_agents_are_told_not_to_repeat_the_previous_answer(self):
        o, st = self.first_run([ms.plan(), CONT_PLAN], [write("INCONCLUSIVE"), write("NO-GO")], [ms.review("COMPLETE"), ms.review("COMPLETE")])
        prepare_continuation(self.runs, o.task_id, "")
        st2 = self.resume(o)
        cont = self.agents[0].calls[1]
        self.assertEqual(cont["continuation"]["note"], "")
        self.assertIn("No additional instructions: continue the unfinished work", cont["task"])
        self.assertIn("Do not simply repeat the previous final answer", cont["task"])
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])


class TestSinglePassRevision(ContinuationBase):
    """The owner's 'one focused revision, one review, then stop' continuation."""
    def start(self, reviews):
        o, st = self.first_run([ms.plan()], [write("INCONCLUSIVE"), write("INCONCLUSIVE", "\nתוקן: C5, IBKR, ספין-אופים.\n")],
                               [ms.review("COMPLETE"), *reviews])
        self.before = {"planner": len(self.agents[0].calls), "claude": len(self.agents[1].calls), "calls": dict(st["calls"])}
        prepare_continuation(self.runs, o.task_id, NOTE, single_pass=True)
        return o, self.resume(o)

    def test_changes_requested_after_the_single_review_stop_for_the_owner(self):
        o, st = self.start([ms.review("CONTINUE", findings=["C5 still uses same-day closes"], next_instruction="Fix C5 again.")])
        self.assertEqual(st["status"], "PAUSED")
        self.assertTrue(st["stop_reason"].startswith("review_changes_requested: C5 still uses same-day closes"))
        self.assertEqual(len(self.agents[0].calls), self.before["planner"], "no planning call")
        self.assertEqual(len(self.agents[1].calls), self.before["claude"] + 1, "Claude revised exactly once")
        self.assertEqual(st["calls"], {"codex": self.before["calls"]["codex"] + 1, "claude": self.before["calls"]["claude"] + 1})
        first = self.agents[1].calls[-1]["instruction"]
        self.assertTrue(first.startswith("OWNER'S INSTRUCTIONS FOR THIS SINGLE REVISION:\n" + NOTE))
        self.assertIn("ONE focused revision pass", first)
        self.assertNotIn("single_pass", st, "the single pass is used up")
        self.assertEqual(st["instruction"], "Fix C5 again.", "Codex's new request is kept for the owner, not executed")
        self.assertTrue(st["continuations"][-1]["single_pass"])

    def test_approval_after_the_single_review_finishes(self):
        o, st = self.start([ms.review("COMPLETE")])
        self.assertEqual(st["status"], "INCONCLUSIVE", "the reviewed verdict is kept unless new evidence changed it")
        self.assertEqual(len(self.agents[1].calls), self.before["claude"] + 1)

    def test_a_technical_review_failure_pauses_and_stays_retryable(self):
        o, st = self.start([{"error": "process", "message": "network"}, ms.review("CONTINUE", findings=["x"], next_instruction="Recheck the C5 entry rule.")])
        self.assertEqual(st["status"], "PAUSED")
        self.assertTrue(st["stop_reason"].startswith("review_failed (process)"))
        st2 = self.resume(o)  # the owner retries the review only
        self.assertEqual(st2["status"], "PAUSED")
        self.assertTrue(st2["stop_reason"].startswith("review_changes_requested"))
        self.assertEqual(len(self.agents[1].calls), self.before["claude"] + 1, "still only the one revision")


class TestBudgetAndSafety(ContinuationBase):
    def inconclusive(self, budget=None, planner=None):
        return self.first_run(planner or [ms.plan(), CONT_PLAN], [write("INCONCLUSIVE"), write("PIVOT")],
                              [ms.review("COMPLETE"), ms.review("COMPLETE")], budget)

    def test_remaining_budget_is_what_was_authorized_minus_what_was_used(self):
        o, st = self.inconclusive({"max_minutes": 120, "max_codex_calls": 25, "max_claude_calls": 20})
        s = self.state(o)
        s["elapsed_sec"] = 41 * 60
        atomic_write_json(o.state_path, s)
        entry = prepare_continuation(self.runs, o.task_id, NOTE)
        self.assertEqual(entry["remaining"]["minutes"], 79.0)
        self.assertEqual(entry["remaining"]["codex"], 25 - st["calls"]["codex"])
        self.assertEqual(self.state(o)["budget"]["max_minutes"], 120, "never reset or silently enlarged")

    def test_an_exhausted_budget_needs_an_explicit_extension(self):
        o, st = self.inconclusive({"max_minutes": 60})
        s = self.state(o)
        s["elapsed_sec"] = 60 * 60.0
        atomic_write_json(o.state_path, s)
        with self.assertRaises(OrchestratorError) as e:
            prepare_continuation(self.runs, o.task_id, NOTE)
        self.assertTrue(str(e.exception).startswith("budget_insufficient: minutes left 0.0"))
        self.assertEqual(self.state(o)["status"], "INCONCLUSIVE", "a refused continuation changes nothing")
        entry = prepare_continuation(self.runs, o.task_id, NOTE, {"max_minutes": 30})
        s = self.state(o)
        self.assertEqual((s["budget"]["max_minutes"], s["elapsed_sec"]), (90, 3600.0))
        self.assertEqual((entry["budget_before"]["max_minutes"], entry["extension"]), (60, {"max_minutes": 30}))
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])

    def test_one_continuation_at_a_time_and_a_restart_finishes_the_authorized_one(self):
        o, st = self.inconclusive()
        prepare_continuation(self.runs, o.task_id, NOTE)
        with self.assertRaises(OrchestratorError) as e:
            prepare_continuation(self.runs, o.task_id, "again")
        self.assertIn("continuation_already_pending", str(e.exception))
        s = self.state(o)
        s["status"] = "RUNNING"  # the server died right after the owner's confirmation, before Codex planned
        atomic_write_json(o.state_path, s)
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        self.assertEqual(len(st2["continuations"]), 1)
        self.assertIsNone(st2["continuation_pending"])

    def test_a_leftover_stop_does_not_cancel_the_new_continuation_and_permissions_are_unchanged(self):
        o, st = self.inconclusive()
        s = self.state(o)
        s.update(approved_tools=["Bash(npm test)"], local_execution=True)
        atomic_write_json(o.state_path, s)
        write_control(o.run_dir, "stop")  # how the task was ended earlier
        prepare_continuation(self.runs, o.task_id, NOTE)
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        self.assertEqual((st2["approved_tools"], st2["local_execution"], st2["kind"], st2["mode"]),
                         (["Bash(npm test)"], True, "research", "goal"))

    def test_only_goal_mode_tasks_can_be_continued(self):
        ad = make_mock_agents({"planner": [ms.plan()], "executor": [ms.write_step(ms.GOOD)], "reviewer": [ms.review("COMPLETE")]})
        o = orch.Orchestrator(self.cfg(), self.ws, self.runs, *ad, out=self.lines.append)
        o.start("implement calc.add")
        with self.assertRaises(OrchestratorError) as e:
            prepare_continuation(self.runs, o.task_id, NOTE)
        self.assertTrue(str(e.exception).startswith("not_goal_mode"))


class TestContinuationFromTheBrowser(AppBase):
    def research(self):
        self.fr.execs[:] = [PLAN, cx(ms.review("COMPLETE", findings=["ok"], next_instruction="", question=""))]
        self.sim.steps[:] = [write("INCONCLUSIVE")]
        _, tid = self.start_task(task=MEMORY_TASK, settings={"mode": "goal", "kind": "research"})
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "INCONCLUSIVE")
        return tid

    def test_the_owner_continues_with_instructions_once_and_codex_plans_from_the_record(self):
        tid = self.research()
        self.assertEqual(self.post("/api/app/continue", {"task_id": tid, "note": NOTE})[0], 400, "explicit confirmation required")
        self.sim = Gate([write("PIVOT")])
        self.fr.execs[:] = [cx(CONT_PLAN | {"user_summary_he": ""}), cx(ms.review("COMPLETE", findings=["ok"], next_instruction="", question=""))]
        st, r = self.post("/api/app/continue", {"task_id": tid, "note": NOTE, "confirm": True})
        self.assertEqual((st, r.get("continuation")), (200, 1), r)
        self.assertTrue(self.sim.entered.wait(10))
        self.assertEqual(self.post("/api/app/continue", {"task_id": tid, "note": "x", "confirm": True})[0], 409, "no duplicate run")
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 409)
        snap = self.get(f"/api/snapshot?task={tid}")[1]["status"]
        self.assertEqual((snap["run_state"], snap["continuations"][0]["note"]), ("active", NOTE))
        self.sim.release.set()
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual(s["status"], "COMPLETE", s["stop_reason"])
        plan_prompt = self.fr.exec_calls()[2]["kw"]["stdin_text"]
        for needed in ("CONTINUATION PLANNING", NOTE, '"verdict": "INCONCLUSIVE"', "never widen them"):
            self.assertIn(needed, plan_prompt)
        claude = self.claude_calls()[-1]
        self.assertIn("--session-id", claude["argv"], "this test's session log is not on disk: a new session, honestly reported")
        self.assertIn("This is a NEW session", claude["kw"]["stdin_text"])
        self.assertIn(NOTE, claude["kw"]["stdin_text"])
        self.assertIn(MEMORY_TASK.strip()[:40], claude["kw"]["stdin_text"], "a new session gets the full task")
        notices = [json.loads(x)["content"] for x in (self.runs / tid / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
                   if json.loads(x)["kind"] == "notice"]
        self.assertTrue(any("in a NEW session with the full task" in n for n in notices))

    def test_after_a_server_restart_the_authorized_continuation_resumes_from_the_browser(self):
        tid = self.research()
        prepare_continuation(self.runs, tid, NOTE)  # confirmed, then the application closed before anything started
        self.srv.stop()
        self.svc = app_service.AppService(str(self.cfg_path), agent_factory=self.agents, workspaces_dir=self.wsdir, open_folder=self.opened.append)
        self.srv = wv.ViewerServer(self.runs, 0, app=self.svc).start()
        self.addCleanup(self.srv.stop)
        self.assertEqual(self.get(f"/api/snapshot?task={tid}")[1]["status"]["continuation_pending"], 1)
        self.fr.execs[:] = [cx(CONT_PLAN | {"user_summary_he": ""}), cx(ms.review("COMPLETE", findings=["ok"], next_instruction="", question=""))]
        self.sim.steps[:] = [write("PIVOT")]
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 200)
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "COMPLETE", self.state(tid)["stop_reason"])

    def test_mock_tasks_cannot_be_continued_with_real_agents(self):
        ad = make_mock_agents({"planner": [ms.plan()], "executor": [write("INCONCLUSIVE")], "reviewer": [ms.review("COMPLETE")]})
        ws = self.base / "mockws"
        ws.mkdir()
        o = orch.Orchestrator(orch.load_config(str(self.cfg_path)), ws, self.runs, *ad, out=lambda m: None, goal=gm.make_goal("goal", "research"))
        o.start(TASK)
        self.assertEqual(self.post("/api/app/continue", {"task_id": o.task_id, "confirm": True})[0], 409)


class TestSameClaudeSession(LongCallBase):
    def test_claude_continues_its_own_session_when_it_still_exists(self):
        o, st = self.run_goal([cx(ms.plan()), cx(ms.review("COMPLETE", findings=["ok"], next_instruction="", question=""))],
                              [dict(write("INCONCLUSIVE"), wait=0.3, log=[("WebSearch", "momentum edge")])], kind="research", task=TASK)
        self.assertEqual(st["status"], "INCONCLUSIVE", st["stop_reason"])
        sid = st["executor_session_id"]
        prepare_continuation(self.runs, o.task_id, NOTE)
        self.fr.execs[:] = [cx(CONT_PLAN | {"user_summary_he": ""}), cx(ms.review("COMPLETE", findings=["ok"], next_instruction="", question=""))]
        self.sim.steps[:] = [write("PIVOT")]
        st2 = orch.Orchestrator(self.cfg_, self.ws, self.runs, *self.agents, out=self.lines.append, task_id=o.task_id).resume()
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        argv = self.claude_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], sid, "the same Claude Code session continues, with its context")
        self.assertIn("Your previous work in this session is still available", self.sim.prompts[-1])
        self.assertEqual(st2["continuations"][0]["context"], "same_session")


class TestContinuePageLogic(unittest.TestCase):
    def test_budget_figures_offer_rules_and_wording(self):
        from test_owner_ux import HTML, js_const, js_function, run_js as _run  # noqa: F401
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        src = "\n".join(["const APP = true;", js_const("STATUS_HE"), js_const("CONTINUABLE"), js_function("contBudget"), js_function("canContinue"),
                         js_function("humanReason"), """console.log(JSON.stringify([
          contBudget({budget: {max_minutes: 120, max_codex_calls: 25, max_claude_calls: 20}, elapsed_sec: 41.2 * 60, calls: {codex: 9, claude: 8}}, null),
          contBudget({budget: {max_minutes: 60, max_codex_calls: 25, max_claude_calls: 20}, elapsed_sec: 3600, calls: {codex: 9, claude: 8}}, null).enough,
          contBudget({budget: {max_minutes: 60, max_codex_calls: 25, max_claude_calls: 20}, elapsed_sec: 3600, calls: {codex: 9, claude: 8}}, {max_minutes: 30}),
          [{status: "INCONCLUSIVE", run_state: "saved"}, {status: "COMPLETE", run_state: "saved", is_mock: true}, {status: "RUNNING", run_state: "active"},
           {status: "STOPPED", run_state: "saved"}, {status: "PAUSED", run_state: "saved", continuation_pending: 1}].map(canContinue),
          humanReason({stop_reason: "owner_continuation: #1"})]));"""])
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(src)
        try:
            r = subprocess.run([node, f.name], capture_output=True, text=True, encoding="utf-8", timeout=60)
        finally:
            Path(f.name).unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        first, exhausted, extended, offer, reason = json.loads(r.stdout)
        self.assertEqual((first["authorized"]["minutes"], first["used"]["minutes"], first["left"]["minutes"]), (120, 41.2, 78.8))
        self.assertEqual((first["left"]["codex"], first["left"]["claude"], first["enough"]), (16, 12, True))
        self.assertFalse(exhausted, "an exhausted budget is not enough to continue")
        self.assertEqual((extended["after"]["minutes"], extended["left"]["minutes"], extended["enough"]), (90, 30.0, True))
        self.assertEqual(offer, [True, False, False, True, False], "offered on saved tasks; never on mock, running or already-authorized")
        self.assertIn("אישרת להמשיך את המשימה", reason)
        for needed in ("המשך משימה", "הנחיות נוספות (לא חובה)", "אשר והמשך", "מה יקרה אחרי האישור:", "שום מונה לא מתאפס",
                       "המשכת את המשימה", "Claude ממשיך באותה שיחה", "השיחה הקודמת של Claude לא נמצאה", "/api/app/continue"):
            self.assertIn(needed, HTML)
        self.assertIn('$("replayBtn").onclick', HTML, "replay (client-side playback of saved messages) is unchanged")


if __name__ == "__main__":
    unittest.main()
