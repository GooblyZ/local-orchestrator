"""Claude's report rejected for FORMAT (e.g. a 4,062-character summary against an unstated 4,000 limit): precise errors,
stated limits, bounded same-session correction that never redoes research, preserved files, budget accounting, and recovery
of a run that failed this way through re-validation + Codex's review (Claude is not re-run). SIMULATED CLIs; no model."""
import hashlib
import json
import unittest

from helpers import ms, orch  # noqa: F401
from test_app import AppBase
from test_long_calls import HEBREW_REPORT, MEMORY_TASK, LongCallBase
from test_mvp import PLAN, cx

import agents  # noqa: E402
import claude_adapter as cla  # noqa: E402
import goal_mode as gm  # noqa: E402
from agents import AdapterError  # noqa: E402
from taskctl import extend_task_budget  # noqa: E402

REPORT = HEBREW_REPORT.format(verdict="PIVOT")
LEDGER = "# Research ledger\n§8c: recorded by the orchestrator from Codex's independent review.\nLEDGER-END-5b1\n"
COMPLETE = cx(ms.review("COMPLETE", findings=["Verified the decisive claims against primary sources."], next_instruction="", question=""))


def summary(n):
    return ("Cycle 15: corrected section A, re-read S9 in full and updated the ledger. " * 200)[:n]


def step(n, write=True):
    rep = {"summary": summary(n), "changed_files": [gm.RESEARCH_REPORT, "research/LEDGER.md"] if write else [],
           "commands_run": [], "remaining_issues": [], "sources": []}
    ops = [{"op": "write", "path": gm.RESEARCH_REPORT, "content": REPORT},
           {"op": "write", "path": "research/LEDGER.md", "content": LEDGER}] if write else []
    return {"ops": ops, "report": rep}


class TestReportValidation(unittest.TestCase):
    def check(self, **over):
        return agents.validate_report(dict({"summary": "s", "changed_files": [], "commands_run": [], "remaining_issues": []}, **over), None)

    def test_the_real_4062_character_summary_is_accepted_and_errors_are_precise(self):
        self.assertEqual(len(self.check(summary=summary(4062))[0]["summary"]), 4062)
        cases = {"field 'summary' invalid: 12,001 characters; the maximum is 12,000": {"summary": "x" * 12_001},
                 "field 'summary' invalid: it must be a string": {"summary": ["x"]},
                 "field 'remaining_issues' invalid: item 2 has 1,001 characters; the maximum is 1,000": {"remaining_issues": ["a", "b" * 1001]},
                 "field 'changed_files' invalid: it must be a list of strings": {"changed_files": "a.md"}}
        for msg, over in cases.items():
            with self.subTest(msg):
                with self.assertRaises(AdapterError) as e:
                    self.check(**over)
                self.assertEqual(e.exception.message, msg)

    def test_the_limits_are_stated_to_claude_and_the_correction_prompt_asks_only_for_the_report(self):
        cli = cla.ClaudeCli.__new__(cla.ClaudeCli)
        cli.cl, cli.max_cycles = {"allowed_tools": ["Read"]}, 8
        text = cli.render_prompt({"task": "t", "cycle": 1, "instruction": "i"})
        self.assertIn(agents.REPORT_LIMITS_TEXT, text)
        fix = cli.render_prompt({"task": MEMORY_TASK, "cycle": 15, "instruction": "i",
                                 "format_correction": {"error": "field 'summary' invalid: 12,001 characters; the maximum is 12,000"}})
        for needed in ("rejected by the orchestrator's FORMAT", "not for its content", "12,001 characters",
                       "Do NOT repeat any research, tool use or file edits", agents.REPORT_LIMITS_TEXT, "Do not add findings"):
            self.assertIn(needed, " ".join(fix.split()))
        self.assertNotIn("AUTONOMOUS DEEP RESEARCH", fix, "the task and the research are not sent again")


class TestBoundedCorrection(LongCallBase):
    def digest(self):
        return {p: hashlib.sha256((self.ws / p).read_bytes()).hexdigest() for p in (gm.RESEARCH_REPORT, "research/LEDGER.md")}

    def test_an_overlong_summary_is_corrected_in_the_same_session_without_redoing_the_work(self):
        o, st = self.run_goal([PLAN, COMPLETE], [step(13_000), step(3_000, write=False)], kind="research", task=MEMORY_TASK)
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        calls = self.claude_calls()
        self.assertEqual(len(calls), 2)
        first, second = calls[0]["argv"], calls[1]["argv"]
        self.assertEqual(second[second.index("--resume") + 1], first[first.index("--session-id") + 1], "same session")
        self.assertIn("FORMAT CORRECTION", self.sim.prompts[1])
        self.assertIn("13,000 characters; the maximum is 12,000", self.sim.prompts[1])
        self.assertEqual(st["calls"]["claude"], 2, "the correction is a counted Claude call")
        self.assertEqual((self.ws / "research/LEDGER.md").read_text(encoding="utf-8"), LEDGER, "the research files are kept")
        kinds = [m["kind"] for m in self.msgs(o) if m["cycle"] == 1]
        self.assertEqual(kinds[:4], ["instruction", "malformed_output", "notice", "report"])
        self.assertIn("rejected for its FORMAT, not its content", [m["content"] for m in self.msgs(o) if m["kind"] == "notice"][0])

    def test_corrections_are_bounded_and_the_failed_run_recovers_with_codexs_review_not_with_claude(self):
        old = dict(agents.REPORT_LIMITS)
        agents.REPORT_LIMITS["summary"] = 4000  # the rule in force when the real run failed
        try:
            o, st = self.run_goal([PLAN, COMPLETE], [step(4062), step(4062, write=False), step(4062, write=False)],
                                  kind="research", task=MEMORY_TASK)
        finally:
            agents.REPORT_LIMITS.update(old)
        self.assertEqual(st["status"], "FAILED")
        self.assertEqual(st["stop_reason"], "malformed_output: field 'summary' invalid: 4,062 characters; the maximum is 4,000 "
                                            "(after 2 format correction requests)")
        self.assertEqual(st["calls"]["claude"], 3, "1 report + 2 corrections, then stop")
        files, sid = self.digest(), st["executor_session_id"]
        self.assertTrue(orch.format_recoverable(st))
        st2 = orch.Orchestrator(self.cfg_, self.ws, self.runs, *self.agents, out=self.lines.append, task_id=o.task_id).resume()
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        self.assertEqual(len(self.claude_calls()), 3, "Claude was not re-run")
        self.assertEqual(st2["history"][-1]["executor"], "review-only")
        self.assertEqual(self.digest(), files)
        self.assertEqual((st2["executor_session_id"], st2["task"]), (sid, MEMORY_TASK))
        recovered = [m for m in self.msgs(o) if m["kind"] == "report" and (m.get("meta") or {}).get("recovered")]
        self.assertEqual(len(recovered), 1, "the rejected report re-validated under the current rules, unedited")
        self.assertEqual(json.loads(recovered[0]["content"])["summary"], summary(4062))
        review = self.fr.exec_calls()[-1]["kw"]["stdin_text"]
        self.assertIn(summary(4062)[:200], review)
        self.assertIn("LEDGER-END-5b1", review, "Codex reviews the complete files")

    def test_corrections_respect_the_owners_claude_budget(self):
        o, st = self.run_goal([PLAN, COMPLETE], [step(13_000), step(3_000, write=False)], budget={"max_claude_calls": 1},
                              kind="research", task=MEMORY_TASK)
        self.assertEqual((st["status"], st["stop_reason"]), ("PAUSED", "budget_reached: claude calls"))
        self.assertTrue(st["review_pending"])
        self.assertEqual((self.ws / "research/LEDGER.md").read_text(encoding="utf-8"), LEDGER)
        extend_task_budget(self.runs, o.task_id, {"max_claude_calls": 5})
        st2 = orch.Orchestrator(self.cfg_, self.ws, self.runs, *self.agents, out=self.lines.append, task_id=o.task_id).resume()
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        self.assertEqual(len(self.claude_calls()), 1, "the work was reviewed, not redone")


class TestRecoveryFromTheBrowser(AppBase):
    def test_a_format_failed_research_run_can_be_continued_from_the_browser(self):
        self.fr.execs[:] = [PLAN, COMPLETE]
        self.sim.steps[:] = [step(13_000)] + [step(13_000, write=False)] * 2
        _, tid = self.start_task(task=MEMORY_TASK, settings={"mode": "goal", "kind": "research"})
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "FAILED")
        snap = self.get(f"/api/snapshot?task={tid}")[1]["status"]
        self.assertTrue(snap["stop_reason"].startswith("malformed_output"))
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual((s["status"], s["calls"]["claude"]), ("COMPLETE", 3), s["stop_reason"])
        html = (orch.Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for needed in ("זו אינה הכרעה על התוכן", "התוכן המדעי לא נשפט ולא הופרך", "המשך: בדיקת Codex לעבודה הקיימת",
                       "נעצר בגלל שגיאת פורמט בתשובה — המחקר והקבצים נשמרו, והתוכן לא נפסל"):
            self.assertIn(needed, html)


if __name__ == "__main__":
    unittest.main()
