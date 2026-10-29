"""Codex's research review: the COMPLETE report and research files (never silently truncated), part-by-part review with
traceable coverage when the material is large, Codex's own verdict as the independent approval, read-only web search for the
reviewer, and a review-only continuation after the reviewer asked the owner (no Claude call, counted in the Codex budget).
Mock agents and SIMULATED CLIs only; no model is contacted."""
import hashlib
import json
import unittest
from pathlib import Path

from helpers import ms, orch  # noqa: F401
from test_codex_adapter import FakeRunner, base_events
from test_goal import GoalBase
from test_long_calls import HEBREW_REPORT, MEMORY_TASK, LongCallBase
from test_mvp import cx

import codex_adapter as ca  # noqa: E402
import goal_mode as gm  # noqa: E402
import research_review as rr  # noqa: E402
from agents import AdapterError  # noqa: E402
from taskctl import extend_task_budget  # noqa: E402


def big_report(sections=60, end="END-OF-REPORT-MARKER-91c"):
    body = "".join(f"## סעיף {i}: ניתוח מתחרים ומקורות\n" + ("טענה מאומתת עם מקור ראשוני ותאריך. " * 25) + "\n\n" for i in range(sections))
    return "# דוח מחקר\n" + body + "## מקורות\n- https://arxiv.org/abs/2501.13956 (נבדק 2026-10-13): Zep.\nVerdict: PIVOT\n" + end + "\n"


def files_step(report, extra=None):
    ops = [{"op": "write", "path": gm.RESEARCH_REPORT, "content": report}]
    for path, text in (extra or {}).items():
        ops.append({"op": "write", "path": path, "content": text})
    return {"ops": ops}


SOURCES = "# Sources ledger\n" + "".join(f"- S{i}: https://example.org/paper{i} (retrieved 2026-10-13) supports claim {i}\n" for i in range(80)) \
          + "SOURCES-LAST-LINE-7d2\n"


class TestMaterialCoverage(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.ws = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.ws, ignore_errors=True))
        (self.ws / gm.RESEARCH_REPORT).write_text(big_report(), encoding="utf-8")
        (self.ws / "research").mkdir()
        (self.ws / "research" / "SOURCES.md").write_text(SOURCES, encoding="utf-8")
        (self.ws / "research" / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
        (self.ws / ".orchestrator").mkdir()
        (self.ws / ".orchestrator" / "TASK.md").write_text("the task", encoding="utf-8")

    def test_every_character_is_covered_exactly_once_and_nothing_is_dropped_silently(self):
        for budget in (20_000, 50_000, 1_000_000):
            with self.subTest(budget=budget):
                m = rr.prepare(self.ws, budget)
                segs = [s for p in m["parts"] for s in p]
                self.assertTrue(all(sum(s["chars"] for s in p) <= budget for p in m["parts"]))
                for f in ("RESEARCH_REPORT.md", "research/SOURCES.md"):
                    text = (self.ws / f).read_bytes().decode("utf-8")  # exact bytes (CRLF on Windows) are kept
                    self.assertEqual("".join(s["text"] for s in segs if s["path"] == f), text, "reassembles exactly")
                self.assertEqual(segs[0]["path"], gm.RESEARCH_REPORT, "the report comes first")
                self.assertEqual(m["manifest"]["not_included"], [{"path": "research/chart.png", "reason": "not UTF-8 text (binary)"}])
                self.assertNotIn(".orchestrator/TASK.md", [f["path"] for f in m["manifest"]["files"]])
                self.assertEqual(m["manifest"]["segments"], len(segs))
        self.assertGreater(rr.prepare(self.ws, 20_000)["manifest"]["parts"], 1)
        self.assertEqual(rr.prepare(self.ws, 1_000_000)["manifest"]["parts"], 1)


class TestReviewerPrompt(unittest.TestCase):
    def reviewer(self, prompts, web=True):
        cli = ca.CodexCli.__new__(ca.CodexCli)
        cli.cx = {"max_prompt_chars": 400_000, "web_search": web}
        from safety import Redactor
        cli.redactor = Redactor()
        cli.call = lambda role, prompt, schema: prompts.append(prompt) or "{}"
        return ca.CodexReviewer(cli)

    def ctx(self, material, mode="research"):
        return {"task": MEMORY_TASK, "cycle": 14, "acceptance_criteria": ["הדוח כתוב בעברית ומפרט את כל המקורות עם תאריכים."],
                "executor_report_UNTRUSTED": {}, "report_discrepancy": {},
                "evidence": {"verification": {"mode": mode}, **({"research_material": material} if material else {})}}

    def test_the_complete_material_and_the_reviewers_role_reach_codex(self):
        prompts = []
        report = big_report()
        self.reviewer(prompts).review(self.ctx({"mode": "complete_in_one_call", "segments": [{"id": "S1", "text": report}]}))
        p = prompts[0]
        self.assertIn("END-OF-REPORT-MARKER-91c", p, "the end of a long report is not cut off")
        self.assertNotIn("[truncated", p)
        self.assertIn("הדוח כתוב בעברית", p, "criteria are readable, not escaped")
        self.assertIn(ca.WEB_ONLY, p)
        for needed in ("YOU are the independent Codex reviewer", "IS Codex's independent review and approval",
                       "never ask the implementation agent to write, record or simulate Codex's approval",
                       "NOT scientifically validated", "reject weak or unsupported findings"):
            self.assertIn(needed, p)

    def test_oversized_material_is_an_error_never_a_silent_cut(self):
        with self.assertRaises(AdapterError) as e:
            self.reviewer([]).review(self.ctx({"segments": [{"id": "S1", "text": "x" * 450_000}]}))
        self.assertIn("exceeds the prompt budget", e.exception.message)

    def test_code_reviews_keep_no_tools(self):
        prompts = []
        self.reviewer(prompts).review(self.ctx(None, mode="native"))
        self.assertIn(ca.NO_TOOLS, prompts[0])
        self.assertNotIn(ca.WEB_ONLY, prompts[0])

    def test_claude_and_the_planner_never_simulate_codexs_approval(self):
        self.assertIn("Never write, claim or simulate Codex's review, decision or approval", " ".join(gm.research_rules(MEMORY_TASK).split()))
        self.assertIn("never write a criterion that requires the implementation agent to record or simulate Codex's approval",
                      gm.planner_research_note(MEMORY_TASK))


class TestResearchReviewFlow(GoalBase):
    def run_research(self, steps, reviews, budget=None, cfg_over=None):
        return self.goal_run(steps, reviews, budget=budget, kind="research", task=MEMORY_TASK, cfg_over=cfg_over)

    def reviewer_calls(self):
        return [c for c in self.agents[2].calls if not c.get("part_review")]

    def test_small_material_is_reviewed_complete_in_one_call_and_kept_out_of_the_transcript(self):
        o, st = self.run_research([files_step(big_report(), {"research/SOURCES.md": SOURCES})], [ms.review("COMPLETE")])
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        rm = self.reviewer_calls()[0]["evidence"]["research_material"]
        self.assertEqual(rm["mode"], "complete_in_one_call")
        texts = "".join(s["text"] for s in rm["segments"])
        self.assertIn("END-OF-REPORT-MARKER-91c", texts)
        self.assertIn("SOURCES-LAST-LINE-7d2", texts, "every research file, not only the report")
        self.assertEqual(st["calls"]["codex"], 2)
        tr = (o.run_dir / "transcript.jsonl").read_text(encoding="utf-8")
        self.assertIn("research_material_coverage", tr)
        self.assertNotIn("SOURCES-LAST-LINE-7d2", tr, "the transcript keeps the coverage manifest, not the full texts")

    def test_large_material_is_reviewed_part_by_part_with_counted_calls_and_traceable_coverage(self):
        o, st = self.run_research([files_step(big_report(120), {"research/SOURCES.md": SOURCES})], [ms.review("COMPLETE")],
                                  cfg_over={"review_material_chars": 20_000})
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        parts = [c for c in self.agents[2].calls if c.get("part_review")]
        final = self.reviewer_calls()[0]["evidence"]["research_material"]
        n = final["parts"]
        self.assertGreater(n, 2)
        self.assertEqual(len(parts), n - 1)
        self.assertEqual(len(final["your_notes_from_earlier_parts"]), n - 1)
        seen = [s["id"] for c in parts for s in c["segments"]] + [s["id"] for s in final["segments"]]
        self.assertEqual(seen, [f"S{i}" for i in range(1, final["coverage"]["segments"] + 1)], "every segment exactly once")
        self.assertEqual(st["calls"]["codex"], 1 + n, "planner + one call per part: all counted")
        ev = [json.loads(x) for x in (o.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len([e for e in ev if e["event"] == "review_part"]), n - 1)
        self.assertEqual([e["data"]["unconfirmed"] for e in ev if e["event"] == "review_material"], [[]])

    def test_resume_after_the_reviewer_asked_the_owner_starts_with_a_review_not_with_claude(self):
        ask = ms.review("HUMAN_REQUIRED", question="Please route a review cycle to Codex.")
        o, st = self.run_research([files_step(big_report(), {"research/SOURCES.md": SOURCES})], [ask, ms.review("COMPLETE")])
        self.assertEqual((st["status"], st["stop_reason"]), ("HUMAN_REQUIRED", "reviewer_requested_human"))
        files_before = {p: hashlib.sha256((self.ws / p).read_bytes()).hexdigest() for p in (gm.RESEARCH_REPORT, "research/SOURCES.md")}
        executor_calls, sid, cycle = len(self.agents[1].calls), st["executor_session_id"], st["cycle"]
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        self.assertEqual(len(self.agents[1].calls), executor_calls, "Claude was not re-run")
        self.assertEqual((st2["cycle"], st2["history"][-1]["executor"]), (cycle + 1, "review-only"))
        self.assertEqual(st2["calls"], {"codex": st["calls"]["codex"] + 1, "claude": st["calls"]["claude"]}, "the review is counted")
        self.assertEqual({p: hashlib.sha256((self.ws / p).read_bytes()).hexdigest() for p in files_before}, files_before)
        self.assertEqual((st2["executor_session_id"], st2["task"]), (sid, MEMORY_TASK))
        msgs = [json.loads(x) for x in (o.run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
        last = [m for m in msgs if m["cycle"] == cycle + 1]
        self.assertNotIn("instruction", [m["kind"] for m in last], "no instruction was sent to Claude")
        self.assertIn("Review-only continuation", " ".join(m["content"] for m in last if m["kind"] == "notice"))
        self.assertIn("SOURCES-LAST-LINE-7d2", "".join(s["text"] for s in self.reviewer_calls()[-1]["evidence"]["research_material"]["segments"]))

    def test_after_a_review_only_cycle_weak_findings_are_still_rejected_and_work_continues(self):
        ask = ms.review("HUMAN_REQUIRED", question="q?")
        fix = ms.review("CONTINUE", findings=["S9 is only a search snippet"], next_instruction="Re-read S9 in full and correct section C.")
        o, st = self.run_research([files_step(big_report()), files_step(big_report() + "\nתיקון סעיף C\n")],
                                  [ask, fix, ms.review("COMPLETE")])
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        self.assertEqual([h["executor"] for h in st2["history"]], ["ok", "review-only", "ok"])
        self.assertEqual(st2["history"][1]["decision"], "CONTINUE", "a structurally complete report was not accepted as validated")
        self.assertIn("Re-read S9", self.agents[1].calls[-1]["instruction"])

    def test_the_structure_gate_still_applies_after_a_review_only_cycle(self):
        no_sources = big_report().replace("## מקורות", "## הערות")
        o, st = self.run_research([files_step(no_sources)], [ms.review("HUMAN_REQUIRED", question="q?"), ms.review("COMPLETE")])
        st2 = self.resume(o)
        self.assertNotEqual(st2["status"], "COMPLETE")
        ro = [h for h in st2["history"] if h["executor"] == "review-only"][0]
        self.assertEqual(ro["decision"], "CONTINUE")
        self.assertIn("completion rejected by deterministic gate", " ".join(ro["findings"]))

    def test_the_review_only_continuation_respects_the_owners_codex_budget(self):
        o, st = self.run_research([files_step(big_report())], [ms.review("HUMAN_REQUIRED", question="q?"), ms.review("COMPLETE")],
                                  budget={"max_codex_calls": 2})
        self.assertEqual((st["status"], st["calls"]["codex"]), ("HUMAN_REQUIRED", 2))
        st2 = self.resume(o)
        self.assertEqual((st2["status"], st2["stop_reason"]), ("PAUSED", "budget_reached: codex calls 2/2"))
        self.assertTrue(st2.get("review_pending"), "the review is still owed after the owner extends the budget")
        extend_task_budget(self.runs, o.task_id, {"max_codex_calls": 1})
        st3 = self.resume(o)
        self.assertEqual((st3["status"], st3["calls"]["codex"]), ("COMPLETE", 3), st3["stop_reason"])
        self.assertEqual(st3["history"][-1]["executor"], "review-only")
        self.assertEqual(len(self.agents[1].calls), 1)


class TestReviewerWebSearchEndToEnd(LongCallBase):
    def test_the_research_reviewer_searches_the_web_read_only_and_its_queries_are_recorded(self):
        review = json.dumps(ms.review("COMPLETE", findings=["Verified Zep paper at arxiv.org/abs/2501.13956 (2026-10-13)."]))
        ev = base_events(review)
        ev.insert(2, {"type": "item.completed", "item": {"id": "w1", "type": "web_search", "query": "Zep temporal knowledge graph arXiv"}})
        step = files_step(HEBREW_REPORT.format(verdict="PIVOT"), {"research/SOURCES.md": SOURCES})
        o, st = self.run_goal([cx(ms.plan()), {"final": review, "events": ev}], [step], kind="research", task=MEMORY_TASK)
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        rev_call = self.fr.exec_calls()[1]
        self.assertIn("--search", rev_call["argv"])
        self.assertIn(ca.WEB_ONLY, rev_call["kw"]["stdin_text"])
        self.assertIn("SOURCES-LAST-LINE-7d2", rev_call["kw"]["stdin_text"])
        msgs = [json.loads(x) for x in (o.run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
        rv = [m for m in msgs if m["kind"] == "review"][0]
        self.assertEqual(rv["meta"]["web_searches"][0]["query"], "Zep temporal knowledge graph arXiv")


if __name__ == "__main__":
    unittest.main()
