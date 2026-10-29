"""Long research prompts: a realistic ~120,000-character task (Hebrew, English, Markdown, code, URLs) travels IN FULL from the
browser to both agents over stdin, is kept on disk for the native agent, survives pause/resume/restart, and is not re-sent
on every cycle. Real adapters against SIMULATED CLIs; no model is contacted."""
import json
import unittest

from helpers import ms, orch  # noqa: F401
from test_app import AGAIN, OK_REVIEW, WRITE_GOOD, AppBase, Gate
from test_mvp import PLAN, MvpBase, UnflaggedSim

import app_service  # noqa: E402
import claude_adapter as cla  # noqa: E402
import codex_adapter as ca  # noqa: E402
import conversation  # noqa: E402
import web_viewer as wv  # noqa: E402
from test_codex_adapter import FakeRunner  # noqa: E402

SECTION = """## סעיף {i}: בדיקת השערה {i} — Momentum vs. Mean Reversion

**מטרה:** לבדוק האם קיים יתרון סטטיסטי נטו (אחרי עמלות) באסטרטגיה {i} על נתוני BTC-USD שעתיים.

### Data
- Source: https://api.exchange.example/v2/candles?product=BTC-USD&granularity=3600&page={i}
- Docs: https://docs.example.org/market-data/rest#section-{i}
- תקופה: 2021-01-01 עד 2025-12-31; חלוקה ל-in-sample / out-of-sample (70/30, walk-forward).

### Method
```python
def signal_{i}(close, lookback={lb}):
    r = close.pct_change(lookback)
    return (r > 0).astype(int) - (r < 0).astype(int)  # {{-1, 0, 1}}
```
| Metric | Threshold | הערה |
|---|---|---|
| Sharpe (net) | > 1.0 | אחרי עמלה של 0.1% לכל צד |
| Max drawdown | < 25% | |

> Requirement R-{i}: report the out-of-sample Sharpe, the cost model and the exact parameters; if no edge, say INCONCLUSIVE.

"""


def long_task(target=120_000):
    head = "# משימת מחקר ארוכה — Long research specification\n\nכל הדרישות להלן מחייבות. All requirements below are binding.\n\n"
    parts, i = [head], 0
    while sum(map(len, parts)) < target:
        i += 1
        parts.append(SECTION.format(i=i, lb=5 + i % 20))
    return "".join(parts) + "\n**סוף המפרט — END OF SPECIFICATION (marker 7f3a)**"  # no outer whitespace: the app strips it


TASK = long_task()


class TestTheLongTask(unittest.TestCase):
    def test_the_sample_is_realistic(self):
        self.assertGreater(len(TASK), 100_000)
        for needed in ("משימת מחקר", "```python", "| Metric |", "https://api.exchange.example", "Requirement R-1:", "marker 7f3a"):
            self.assertIn(needed, TASK)


class TestLongTaskThroughTheApp(AppBase):
    def run_long(self, sim=None, execs=(PLAN, OK_REVIEW), steps=(WRITE_GOOD,)):
        if sim is not None:
            self.sim = sim
        self.fr.execs[:] = list(execs)
        self.sim.steps[:] = list(steps)
        return self.start_task(task=TASK)

    def test_a_120k_character_task_reaches_both_agents_in_full_over_stdin(self):
        prep, tid = self.run_long()
        self.assertEqual(prep["task"], TASK, "the confirmation shows the whole task")
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual(s["status"], "COMPLETE", s["stop_reason"])
        self.assertEqual(s["task"], TASK, "state.json keeps the whole task")
        plan_call = self.fr.exec_calls()[0]
        self.assertIn(TASK, plan_call["kw"]["stdin_text"], "the planner receives the complete task, untruncated")
        self.assertLess(sum(len(a) for a in plan_call["argv"]), 4000, "the task never travels on the command line")
        first = self.claude_calls()[0]
        self.assertIn(TASK, first["kw"]["stdin_text"], "Claude's first prompt contains the complete task")
        self.assertLess(sum(len(a) for a in first["argv"]), 8000)
        ws = self.state(tid)["workspace"]
        from pathlib import Path
        self.assertEqual((Path(ws) / ".orchestrator" / "TASK.md").read_text(encoding="utf-8"), TASK)
        self.assertEqual(s["task_file"]["chars"], len(TASK))
        msgs = [json.loads(x) for x in (self.runs / tid / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([m["content"] for m in msgs if m["kind"] == "task"], [TASK], "the transcript keeps the whole task")
        seqs = [m["seq"] for m in msgs]
        self.assertEqual(seqs, sorted(set(seqs)), "message numbering stays unique after a very large message")
        self.assertIn(".orchestrator/TASK.md", [f["path"] for f in self.get(f"/api/app/files?task={tid}")[1]["files"]],
                      "the owner can open the saved task from the files panel")

    def test_later_claude_cycles_reference_the_task_and_every_review_gets_it_complete(self):
        _, tid = self.run_long(execs=(PLAN, AGAIN, OK_REVIEW), steps=(WRITE_GOOD, WRITE_GOOD))
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "COMPLETE", self.state(tid)["stop_reason"])
        second = self.claude_calls()[1]["kw"]["stdin_text"]
        self.assertNotIn(TASK, second, "the resumed Claude session already has the task; it re-reads TASK.md")
        self.assertIn(".orchestrator/TASK.md", second)
        self.assertIn(f"{len(TASK):,} characters", second)
        self.assertLess(len(second), 20_000)
        reviews = [c for c in self.fr.exec_calls()[1:]]
        self.assertEqual(len(reviews), 2)
        for c in reviews:
            self.assertIn("TASK: " + TASK + "\n", c["kw"]["stdin_text"], "each ephemeral review receives the COMPLETE task")
            self.assertIn(json.loads(PLAN["final"])["acceptance_criteria"][0], c["kw"]["stdin_text"])
            self.assertLess(sum(len(a) for a in c["argv"]), 4000, "over stdin, never on the command line")

    def test_critical_instructions_beyond_character_30000_reach_the_reviewer(self):
        critical = ("\n\n## STOPPING CRITERIA AND RESTRICTIONS (binding)\n"
                    "- CRITICAL-ACCEPT-9b1: COMPLETE only if RESEARCH_REPORT.md states the out-of-sample Sharpe after 0.1% costs.\n"
                    "- CRITICAL-STOP-4e7: stop and report INCONCLUSIVE after 3 rejected hypotheses; never test a 4th.\n"
                    "- CRITICAL-RESTRICT-c22: אסור להשתמש בנתונים בתשלום או במפתחות API פרטיים.")
        task = long_task(60_000) + critical
        self.assertGreater(task.index("CRITICAL-ACCEPT-9b1"), 30_000)
        self.fr.execs[:] = [PLAN, AGAIN, OK_REVIEW]
        self.sim.steps[:] = [WRITE_GOOD, WRITE_GOOD]
        _, tid = self.start_task(task=task)
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "COMPLETE", self.state(tid)["stop_reason"])
        prompts = [c["kw"]["stdin_text"] for c in self.fr.exec_calls()]
        self.assertEqual(len(prompts), 3, "planner + two reviews")
        for p in prompts:
            self.assertIn(task, p, "planner and every review see the whole specification, its ending included")
            for marker in ("CRITICAL-ACCEPT-9b1", "CRITICAL-STOP-4e7", "CRITICAL-RESTRICT-c22", "אסור להשתמש בנתונים בתשלום"):
                self.assertIn(marker, p)
            self.assertNotIn("[truncated", p.split("<untrusted_data>")[0], "nothing before the evidence is ever clipped")

    def test_a_maximum_size_task_still_fits_the_review_prompt_with_large_evidence(self):
        import app_service as aps
        reviewer = ca.CodexReviewer(self.fr_cli())
        task = long_task(aps.MAX_TASK_CHARS)[:aps.MAX_TASK_CHARS - 40] + "\nFINAL-RULE-z9: never place trades."
        ctx = {"task": task, "cycle": 3, "acceptance_criteria": ["c%d" % i for i in range(50)],
               "executor_report_UNTRUSTED": {"summary": "s"}, "report_discrepancy": None,
               "evidence": {"changed_files": [{"path": "big.py", "content": "x = 1\n" * 100_000}], "verification": {"mode": "native"}}}
        self.fr.execs[:] = [OK_REVIEW]
        reviewer.review(ctx)
        p = self.fr.exec_calls()[-1]["kw"]["stdin_text"]
        self.assertIn("TASK: " + task + "\n", p)
        self.assertIn("FINAL-RULE-z9", p)
        self.assertIn("[truncated", p, "the evidence, not the task, is what gets shortened")
        self.assertLessEqual(len(p), orch.load_config(None)["codex"]["max_prompt_chars"])

    def fr_cli(self):
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cli = ca.CodexCli(cfg, base_dir=self.proj, runner=self.fr)
        cli.preflight()
        return cli

    def test_a_modified_task_file_is_restored_before_the_next_cycle(self):
        self.sim = Gate([])
        _, tid = self.run_long(execs=(PLAN, AGAIN, OK_REVIEW), steps=(WRITE_GOOD, WRITE_GOOD))
        self.assertTrue(self.sim.entered.wait(10))
        from pathlib import Path
        tp = Path(self.state(tid)["workspace"]) / ".orchestrator" / "TASK.md"
        tp.write_text("tampered", encoding="utf-8")
        self.sim.release.set()
        self.wait_idle()
        self.assertEqual(tp.read_text(encoding="utf-8"), TASK)
        events = (self.runs / tid / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("task_file_restored", events)

    def test_pause_resume_and_a_restarted_app_keep_the_whole_task(self):
        self.sim = Gate([])
        _, tid = self.run_long(execs=(PLAN, AGAIN, OK_REVIEW), steps=(WRITE_GOOD, WRITE_GOOD))
        self.assertTrue(self.sim.entered.wait(10))
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "pause"})[0], 200)
        self.sim.release.set()
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "PAUSED")
        # a fresh app process (new service + server) over the same runs folder
        self.srv.stop()
        self.svc = app_service.AppService(str(self.cfg_path), agent_factory=self.agents, workspaces_dir=self.wsdir,
                                          open_folder=self.opened.append, catalog=lambda cfg: {"codex": [], "claude": {"models": [], "efforts": []}})
        self.srv = wv.ViewerServer(self.runs, 0, app=self.svc).start()
        self.addCleanup(self.srv.stop)
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual((s["status"], s["task"]), ("COMPLETE", TASK), s["stop_reason"])
        self.assertIn(".orchestrator/TASK.md", self.claude_calls()[1]["kw"]["stdin_text"])

    def test_limits_are_explicit_with_accurate_errors(self):
        st, r = self.post("/api/app/prepare", {"task": "א" * 200_000})
        self.assertEqual(st, 200, "200,000 Hebrew characters (~400 KB of UTF-8) are accepted")
        st, r = self.post("/api/app/prepare", {"task": "א" * 200_001})
        self.assertEqual(st, 400)
        self.assertEqual(r["error"], "task_too_long: 200,001 characters; the limit is 200,000")
        st, r = self.req("POST", "/api/app/prepare", raw=b"x" * (wv.MAX_POST_BYTES + 1))
        self.assertEqual(st, 413)
        self.assertEqual(r["error"], f"request_too_large: {wv.MAX_POST_BYTES + 1:,} bytes; the limit is {wv.MAX_POST_BYTES:,}")
        self.assertEqual(self.fr.exec_calls(), [], "no model is called for a refused task")
        src = (wv.Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for needed in ("const MAX_TASK = 200000", "task_too_long", "request_too_large", "הצג את כל המשימה ("):
            self.assertIn(needed, src)


class TestLongTaskLayers(MvpBase):
    def test_the_configured_prompt_limits_fit_the_largest_task(self):
        cfg = orch.load_config(None)
        self.assertGreaterEqual(app_service.MAX_TASK_CHARS, 100_000)
        for agent in ("codex", "claude"):
            self.assertGreater(cfg[agent]["max_prompt_chars"], app_service.MAX_TASK_CHARS + 50_000, agent)
        self.assertGreaterEqual(conversation.MAX_CONTENT, app_service.MAX_TASK_CHARS)
        self.assertGreater(wv.MAX_POST_BYTES, app_service.MAX_TASK_CHARS * 3 * 1.2, "worst case: 3 UTF-8 bytes per character")

    def test_short_tasks_are_unchanged(self):
        cfg = self.full_cfg()
        self.fr, self.sim = FakeRunner([PLAN, OK_REVIEW]), UnflaggedSim([WRITE_GOOD])
        codex = ca.CodexCli(cfg, base_dir=self.proj, runner=self.fr)
        o = orch.Orchestrator(cfg, self.ws, self.runs, ca.CodexPlanner(codex), cla.ClaudeExecutor(cla.ClaudeCli(cfg, runner=self.sim)),
                              ca.CodexReviewer(codex), out=self.lines.append, local_execution=True)
        st = o.start("implement calc.add")
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        self.assertIsNone(st["task_file"])
        self.assertFalse((self.ws / ".orchestrator").exists())
        self.assertIn("implement calc.add", self.prompts()[1], "a short task is still given to the reviewer verbatim")

    def test_seq_numbers_continue_after_a_huge_last_line(self):
        p = self.base / "t.jsonl"
        p.write_text(json.dumps({"seq": 1, "content": "x"}) + "\n" + json.dumps({"seq": 2, "content": "ש" * 300_000}) + "\n",
                     encoding="utf-8")
        self.assertEqual(conversation._last_seq(p), 2)


if __name__ == "__main__":
    unittest.main()
