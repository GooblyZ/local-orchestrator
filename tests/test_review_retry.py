"""The final independent review: the standard-speed guard looks at the CLI's event STRUCTURE (a reviewer's web-search RESULTS
mentioning 'priority' are content, not a service tier), a technical review failure in research never counts as approval and
pauses for a review-only retry, and a task that failed this way can retry ONLY the review. Simulated CLIs / mocks only."""
import hashlib
import json
import unittest

from helpers import ms, orch  # noqa: F401
from test_app import AppBase
from test_codex_adapter import CodexBase, FakeRunner, base_events
from test_goal import Base
from test_long_calls import HEBREW_REPORT
from test_mvp import PLAN, cx

import codex_adapter as ca  # noqa: E402
import goal_mode as gm  # noqa: E402
from agents import AdapterError, make_mock_agents  # noqa: E402
from store import atomic_write_json  # noqa: E402

PLAN_JSON = json.dumps(ms.plan())
SEARCH = {"type": "item.completed", "item": {
    "id": "ws1", "type": "web_search", "query": "site:sec.gov tender offer odd lot priority fewer than 100 shares",
    "action": {"type": "search", "query": "tender offer odd lot priority", "queries": ["odd lot priority proration"]},
    "results": [{"title": "Offer to Purchase - Odd Lots: priority for holders of fewer than 100 shares",
                 "url": "https://www.sec.gov/Archives/edgar/data/1/offer.htm", "snippet": "Odd Lot priority; fast settlement; flex terms"}]}}


class TestStandardSpeedGuard(CodexBase):
    def plan_with(self, *extra, at=2):
        ev = base_events(PLAN_JSON)
        for i, e in enumerate(extra):
            ev.insert(at + i, e)
        cli = self.cli(FakeRunner([{"final": PLAN_JSON, "events": ev}]))
        return ca.CodexPlanner(cli).plan({"task": "t"})

    def test_search_results_mentioning_priority_are_content_not_a_service_tier(self):
        self.assertEqual(json.loads(self.plan_with(SEARCH))["plan"], ms.plan()["plan"], "the failed live review's situation is accepted")
        self.assertEqual(ca.tier_indicators([SEARCH]), [])

    def test_real_tier_signals_are_still_refused(self):
        cases = {"turn event": {"type": "turn.started", "service_tier": "priority"},
                 "inside an item": {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "x", "service_tier": "flex"}},
                 "nested metadata": {"type": "turn.completed", "usage": {"input_tokens": 1}, "model_info": {"speed_tier": "ultrafast"}},
                 "fast mode flag": {"type": "thread.started", "thread_id": "t", "fast_mode": True},
                 "tier word in metadata": {"type": "thread.started", "thread_id": "t", "model": "gpt-6-sol-fast"}}
        for name, e in cases.items():
            with self.subTest(name):
                with self.assertRaises(AdapterError) as err:
                    self.plan_with(e, at=1)
                self.assertIn("service-tier indicator in CLI events; refusing (standard speed only)", err.exception.message)
        self.assertEqual(ca.tier_indicators([{"type": "turn.completed", "service_tier": "default"}]), [], "standard is fine")


def write(v, extra=""):
    return {"ops": [{"op": "write", "path": gm.RESEARCH_REPORT, "content": HEBREW_REPORT.format(verdict=v) + extra}],
            "report": {"summary": "Phase 2 done: report updated with dated sources."}}


class TestTechnicalReviewFailure(Base):
    def run_research(self, reviews, steps=None, kind="research"):
        self.agents = make_mock_agents({"planner": [ms.plan()], "executor": steps or [write("PIVOT")], "reviewer": reviews})
        o = orch.Orchestrator(self.cfg(), self.ws, self.runs, *self.agents, out=self.lines.append, goal=gm.make_goal("goal", kind))
        return o, o.start("# Financial research\nDecision: GO / PIVOT / NO-GO / INCONCLUSIVE.")

    def resume(self, o):
        return orch.Orchestrator(self.cfg(), self.ws, self.runs, *self.agents, out=self.lines.append, task_id=o.task_id).resume()

    def test_it_pauses_without_approval_and_the_retry_reviews_only(self):
        tier = {"error": "process", "message": "service-tier indicator in CLI events; refusing (standard speed only)"}
        o, st = self.run_research([tier, ms.review("COMPLETE")])
        self.assertEqual(st["status"], "PAUSED", "never COMPLETE, never a lost task")
        self.assertTrue(st["stop_reason"].startswith("review_failed (process): service-tier indicator"))
        self.assertEqual((st["history"][-1]["reviewer"], st["history"][-1]["decision"]), ("error", "PAUSED"))
        self.assertTrue(st["review_pending"])
        report = hashlib.sha256((self.ws / gm.RESEARCH_REPORT).read_bytes()).hexdigest()
        executor_calls, codex = len(self.agents[1].calls), st["calls"]["codex"]
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        self.assertEqual(len(self.agents[1].calls), executor_calls, "Claude was not re-run")
        self.assertEqual(st2["calls"]["codex"], codex + 1, "the retried review is counted")
        self.assertEqual(st2["history"][-1]["executor"], "review-only")
        self.assertEqual(hashlib.sha256((self.ws / gm.RESEARCH_REPORT).read_bytes()).hexdigest(), report, "research untouched")
        self.assertEqual(self.agents[2].calls[-1]["executor_report_UNTRUSTED"]["summary"], "Phase 2 done: report updated with dated sources.",
                         "Codex reviews the last real report of the research")

    def test_a_task_that_already_failed_this_way_can_retry_only_the_review(self):
        o, st = self.run_research([{"error": "process", "message": "x"}, ms.review("COMPLETE")])
        s = json.loads(o.state_path.read_text(encoding="utf-8"))  # as the earlier version left it: FAILED
        s.update(status="FAILED", stop_reason="reviewer_error (process): service-tier indicator in CLI events; refusing (standard speed only)")
        s.pop("review_pending", None)
        atomic_write_json(o.state_path, s)
        self.assertTrue(orch.format_recoverable(s))
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        self.assertEqual(len(self.agents[1].calls), 1, "Claude was not re-run")
        self.assertEqual(st2["history"][-1]["executor"], "review-only")

    def failed_then_retry(self, retry_review, extra_steps=()):
        """A research task whose review failed technically, then the owner's 'retry the review only' with `retry_review`."""
        o, st = self.run_research([{"error": "process", "message": "tier"}, retry_review], steps=[write("PIVOT"), *extra_steps])
        self.assertEqual(st["status"], "PAUSED")
        self.before = {"report": hashlib.sha256((self.ws / gm.RESEARCH_REPORT).read_bytes()).hexdigest(), "claude": len(self.agents[1].calls),
                       "calls": dict(st["calls"]), "history": len(st["history"]), "budget": dict(st["budget"])}
        return o, self.resume(o)

    def assert_only_the_review_ran(self, st):
        self.assertEqual(len(self.agents[1].calls), self.before["claude"], "Claude was not started")
        self.assertEqual(st["calls"], {"codex": self.before["calls"]["codex"] + 1, "claude": self.before["calls"]["claude"]})
        self.assertEqual(hashlib.sha256((self.ws / gm.RESEARCH_REPORT).read_bytes()).hexdigest(), self.before["report"], "report untouched")
        self.assertEqual(st["budget"], self.before["budget"])
        self.assertEqual(len(st["history"]), self.before["history"] + 1, "history kept and appended")

    def test_retry_requests_changes_findings_are_kept_and_it_waits_for_the_owner(self):
        ask = ms.review("CONTINUE", findings=["C5 uses an inconsistent event denominator"], next_instruction="Recompute C5 with one denominator.")
        o, st = self.failed_then_retry(ask, extra_steps=[write("PIVOT", "\nC5 fixed\n")])
        self.assertEqual(st["status"], "PAUSED")
        self.assertTrue(st["stop_reason"].startswith("review_changes_requested: C5 uses an inconsistent event denominator"))
        self.assert_only_the_review_ran(st)
        self.assertEqual(st["instruction"], "Recompute C5 with one denominator.", "Codex's request is kept for the owner")
        self.assertEqual(st["history"][-1]["decision"], "CONTINUE")
        st2 = self.resume(o)  # the OWNER's explicit decision: let Claude do what Codex asked
        self.assertEqual(self.agents[1].calls[-1]["instruction"], "Recompute C5 with one denominator.")
        self.assertEqual(len(self.agents[1].calls), self.before["claude"] + 1)

    def test_retry_rejects_the_report_and_it_waits_for_the_owner(self):
        o, st = self.failed_then_retry(ms.review("FAILED", findings=["The edge claim is unsupported by the evidence"]))
        self.assertEqual(st["status"], "PAUSED")
        self.assertTrue(st["stop_reason"].startswith("review_rejected: The edge claim is unsupported"))
        self.assert_only_the_review_ran(st)

    def test_retry_approval_finishes_with_the_reviewed_verdict(self):
        o, st = self.failed_then_retry(ms.review("COMPLETE"))
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        self.assertIn("verdict PIVOT", st["stop_reason"])
        self.assert_only_the_review_ran(st)

    def test_retry_that_fails_technically_again_stays_retryable(self):
        o, st = self.failed_then_retry({"error": "process", "message": "network"})
        self.assertEqual(st["status"], "PAUSED")
        self.assertTrue(st["stop_reason"].startswith("review_failed (process): network"))
        self.assertTrue(st["review_pending"])
        self.assertEqual(len(self.agents[1].calls), self.before["claude"])

    def test_an_approval_refused_by_the_structure_check_also_waits(self):
        bad = {"ops": [{"op": "write", "path": gm.RESEARCH_REPORT, "content": "# report without sources or verdict\n"}], "report": {"summary": "s"}}
        o, st = self.run_research([{"error": "process", "message": "tier"}, ms.review("COMPLETE")], steps=[bad])
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "PAUSED")
        self.assertTrue(st2["stop_reason"].startswith("review_changes_requested: "), st2["stop_reason"])
        self.assertEqual(len(self.agents[1].calls), 1)

    def test_code_tasks_keep_failing_on_a_review_error(self):
        self.agents = make_mock_agents({"planner": [ms.plan()], "executor": [ms.write_step(ms.GOOD)], "reviewer": [{"error": "process"}]})
        o = orch.Orchestrator(self.cfg(), self.ws, self.runs, *self.agents, out=self.lines.append, goal=gm.make_goal("goal", "code"))
        st = o.start("implement calc.add")
        self.assertEqual(st["status"], "FAILED")
        self.assertTrue(st["stop_reason"].startswith("reviewer_error (process)"))


class TestRetryFromTheBrowser(AppBase):
    def test_retry_only_the_review_of_a_failed_research_task(self):
        self.fr.execs[:] = [PLAN, cx(ms.review("COMPLETE", findings=["ok"], next_instruction="", question=""))]
        self.sim.steps[:] = [write("PIVOT")]
        _, tid = self.start_task(task="# Financial research\nDecision: GO / PIVOT / NO-GO / INCONCLUSIVE.", settings={"mode": "goal", "kind": "research"})
        self.wait_idle()
        p = self.runs / tid / "state.json"
        s = json.loads(p.read_text(encoding="utf-8"))
        s.update(status="FAILED", stop_reason="reviewer_error (process): service-tier indicator in CLI events; refusing (standard speed only)")
        atomic_write_json(p, s)
        snap = self.get(f"/api/snapshot?task={tid}")[1]["status"]
        self.assertTrue(snap["needs_owner"])
        claude_before = len(self.claude_calls())
        self.fr.execs[:] = [cx(ms.review("COMPLETE", findings=["Verified against the full report."], next_instruction="", question=""))]
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual(s["status"], "COMPLETE", s["stop_reason"])
        self.assertEqual(len(self.claude_calls()), claude_before, "Claude was not re-run")
        prompt = self.fr.exec_calls()[-1]["kw"]["stdin_text"]
        self.assertIn("research_material", prompt, "the review reads the complete report")
        self.assertIn(ca.WEB_ONLY, prompt)


class TestReviewStatesInThePage(unittest.TestCase):
    def test_a_technical_failure_is_never_shown_as_approval(self):
        from test_owner_ux import HTML, run_js
        out = run_js("""console.log(JSON.stringify([
          humanReason({stop_reason: "review_failed (process): x"}), humanReason({stop_reason: "reviewer_error (process): x"}),
          ownerState({run_state: "saved", status: "PAUSED", kind: "research", stop_reason: "review_failed (process): x"}),
          ownerState({run_state: "saved", status: "FAILED", kind: "research", stop_reason: "reviewer_error (process): x"}),
          ownerState({run_state: "saved", status: "COMPLETE", kind: "research", stop_reason: "research report verified for structure"})]));""")
        self.assertIn("שום דבר לא אושר", out[0])
        self.assertEqual(out[0], out[1])
        self.assertEqual((out[2]["tone"], out[2]["need"], out[2]["title"]), ("need", True, "הביקורת העצמאית נכשלה טכנית — לא ניתן אישור"))
        self.assertEqual(out[3]["title"], out[2]["title"])
        self.assertEqual((out[4]["tone"], out[4]["title"]), ("ok", "הושלם"))
        out2 = run_js("""console.log(JSON.stringify([ownerState({run_state: "saved", status: "PAUSED", kind: "research", stop_reason: "review_changes_requested: x"}),
          ownerState({run_state: "saved", status: "PAUSED", kind: "research", stop_reason: "review_rejected: x"}), humanReason({stop_reason: "review_changes_requested: x"})]));""")
        self.assertEqual((out2[0]["need"], out2[0]["title"]), (True, "Codex ביקש שינויים — ממתין להחלטה שלך"))
        self.assertEqual(out2[1]["title"], "Codex דחה את המחקר — ממתין להחלטה שלך")
        self.assertIn("שום דבר לא הופעל", out2[2])
        for needed in ("שום דבר נוסף לא מתחיל לבד", "המשך: Claude יבצע את השינויים ש-Codex ביקש", "נסה שוב רק את הביקורת העצמאית", "הביקורת העצמאית נכשלה טכנית — לא בוצע אישור",
                       "המחקר נכתב, אבל הביקורת העצמאית נכשלה טכנית — אין אישור", "הביקורת העצמאית של Codex על העבודה האחרונה עדיין לא בוצעה"):
            self.assertIn(needed, HTML)


if __name__ == "__main__":
    unittest.main()
