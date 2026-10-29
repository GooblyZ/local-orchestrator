"""Long model calls in goal mode: no per-call wall-clock limit, observable activity, honest quiet notices, owner stop on a
genuine hang, a safe pause when the owner's overall time budget is reached mid-call, research rubrics taken from the task,
and goal cycles without the legacy cycle cap. Real adapters against SIMULATED CLIs; no model is contacted."""
import json
import re
import sys
import threading
import time
import unittest
from pathlib import Path

from helpers import ms, orch  # noqa: F401
from test_app import OK_REVIEW, WRITE_GOOD, AppBase
from test_codex_adapter import FakeRunner
from test_mvp import PLAN, MvpBase

import claude_adapter as cla  # noqa: E402
import codex_adapter as ca  # noqa: E402
import goal_mode as gm  # noqa: E402
from agents import make_mock_agents  # noqa: E402
from safety import ProcessCancelled, Redactor, run_process  # noqa: E402
from sim_claude import SimClaude  # noqa: E402
from store import write_control  # noqa: E402
from taskctl import extend_task_budget  # noqa: E402

MEMORY_TASK = """# AUTONOMOUS DEEP RESEARCH MISSION — AI Associative Memory Engine
Audit the state of the art and competitors (Mem0, Zep, Letta, HippoRAG), the scientific foundations (episodic memory,
spreading activation), demonstrated technical gaps, prior art and novelty, candidate architectures and retrieval algorithms,
a fair benchmark design, engineering and business feasibility, and a concrete 48-hour experiment plan.
## Executive Decision
GO / PIVOT / NO-GO / INCONCLUSIVE, with confidence and the three strongest reasons. Codex reviews independently.
"""
HEBREW_REPORT = """# דוח מחקר — מנוע זיכרון אסוציאטיבי
## החלטה
ההמלצה: PIVOT לכיוון בנצ'מרק הוגן.
## מקורות
- https://arxiv.org/abs/2501.13956 (נבדק 2026-10-13): ארכיטקטורת Zep.
Verdict: {verdict}
"""


class LongSim(SimClaude):
    """A Claude CLI whose -p call can run for a while ('wait': seconds) or hang until stopped ('hang': True). While running it
    behaves like Claude Code: the session exists from the start and its log (if `log_root`) receives tool-use lines."""
    simulated = False

    def __init__(self, steps, log_root=None, on_wait=None):
        super().__init__(steps)
        self.log_root, self.on_wait = log_root, on_wait

    def _print_mode(self, args, kw):
        step = self.steps[0] if self.steps else {}
        if not (step.get("wait") or step.get("hang")):
            return super()._print_mode(args, kw)
        new = "--session-id" in args
        sid = args[args.index("--session-id" if new else "--resume") + 1]
        self.sessions.setdefault(sid, [])
        cwd = Path(kw["cwd"])
        for op in step.get("partial", []):
            (cwd / op["path"]).write_text(op["content"], encoding="utf-8")
        if self.log_root and step.get("log"):
            d = Path(self.log_root) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
            d.mkdir(parents=True, exist_ok=True)
            with open(d / f"{sid}.jsonl", "a", encoding="utf-8") as f:
                for name, detail in step["log"]:
                    f.write(json.dumps({"type": "assistant", "message": {"content": [
                        {"type": "tool_use", "name": name, "input": {"url" if name == "WebFetch" else "query": detail}}]}}) + "\n")
        if self.on_wait:
            self.on_wait()
        t0 = time.time()
        while step.get("hang") or time.time() - t0 < step["wait"]:
            if kw["cancel_check"]():
                self.steps.pop(0)
                self.prompts.append(kw.get("stdin_text"))
                raise ProcessCancelled("cancelled")
            time.sleep(0.05)
        self.steps[0] = {k: v for k, v in step.items() if k not in ("wait", "log", "partial")}
        if new:
            self.sessions.pop(sid)  # created for real by the normal path below
        return super()._print_mode(args, kw)


class LongCallBase(MvpBase):
    def run_goal(self, codex_execs, steps, budget=None, kind="code", task="implement calc.add", quiet=600, background=False,
                 log=True, on_wait=None):
        cfg = self.full_cfg(quiet_notice_sec=quiet)
        self.log_root = self.base / "claude-config"
        self.fr, self.sim = FakeRunner(codex_execs), LongSim(steps, self.log_root if log else None, on_wait)
        codex = ca.CodexCli(cfg, base_dir=self.proj, runner=self.fr)
        claude = cla.ClaudeCli(cfg, runner=self.sim)
        claude.session_log_root = self.log_root
        self.agents = (ca.CodexPlanner(codex), cla.ClaudeExecutor(claude), ca.CodexReviewer(codex))
        self.cfg_ = cfg
        o = orch.Orchestrator(cfg, self.ws, self.runs, *self.agents, out=self.lines.append, local_execution=True,
                              goal=gm.make_goal("goal", kind, budget))
        self.o = o
        if background:
            t = threading.Thread(target=o.start, args=(task,), daemon=True)
            t.start()
            return o, t
        return o, o.start(task)

    def events(self, o):
        p = o.run_dir / "events.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def activity(self, o):
        return [e["data"] for e in self.events(o) if e["event"] == "activity"]


class TestNoPerCallTimeLimitInGoalMode(LongCallBase):
    def test_both_agents_run_without_a_wall_clock_limit_in_goal_mode(self):
        o, st = self.run_goal([PLAN, OK_REVIEW], [WRITE_GOOD])
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        self.assertEqual([c["kw"]["timeout"] for c in self.fr.exec_calls()], [None, None], "Codex: no per-call limit")
        self.assertEqual([c["kw"]["timeout"] for c in self.claude_calls()], [None], "Claude: no per-call limit")
        self.assertIn("no time limit per call", " ".join(a["text"] for a in self.activity(o)))

    def test_fixed_mode_keeps_its_configured_per_call_timeouts(self):
        cfg = self.full_cfg()
        self.fr, self.sim = FakeRunner([PLAN, OK_REVIEW]), LongSim([WRITE_GOOD])
        codex = ca.CodexCli(cfg, base_dir=self.proj, runner=self.fr)
        o = orch.Orchestrator(cfg, self.ws, self.runs, ca.CodexPlanner(codex), cla.ClaudeExecutor(cla.ClaudeCli(cfg, runner=self.sim)),
                              ca.CodexReviewer(codex), out=self.lines.append, local_execution=True)
        st = o.start("implement calc.add")
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        self.assertEqual({c["kw"]["timeout"] for c in self.fr.exec_calls()}, {cfg["codex"]["timeout_sec"]})
        self.assertEqual({c["kw"]["timeout"] for c in self.claude_calls()}, {cfg["claude"]["timeout_sec"]})

    def test_a_long_quiet_call_is_reported_not_killed_and_its_tool_activity_is_visible(self):
        step = dict(WRITE_GOOD, wait=3.0, log=[("WebSearch", "associative memory benchmarks 2026"),
                                               ("WebFetch", "https://arxiv.org/abs/2501.13956")])
        o, st = self.run_goal([PLAN, OK_REVIEW], [step], quiet=1)
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        texts = [a["text"] for a in self.activity(o)]
        self.assertIn("tool #1: WebSearch associative memory benchmarks 2026", texts)
        self.assertIn("tool #2: WebFetch https://arxiv.org/abs/2501.13956", texts)
        quiet = [a for a in self.activity(o) if a.get("notice") and a["text"].startswith("quiet:")]
        self.assertTrue(quiet, "a quiet period is surfaced to the owner")
        self.assertIn("NOT stopped automatically", quiet[0]["text"])

    def test_research_may_take_many_turns_in_goal_mode(self):
        o, st = self.run_goal([PLAN, OK_REVIEW], [dict(WRITE_GOOD, num_turns=400)])
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])


class TestHangsCancellationAndBudgets(LongCallBase):
    def wait_for(self, cond, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_a_genuine_hang_is_surfaced_and_the_owner_can_stop_it(self):
        o, t = self.run_goal([PLAN], [{"hang": True}], quiet=1, background=True, log=False)
        self.assertTrue(self.wait_for(lambda: any(a.get("notice") and a["text"].startswith("quiet:") for a in self.activity(o))),
                        "the owner is told the call is quiet")
        self.assertTrue(t.is_alive(), "a quiet call is never ended automatically")
        write_control(o.run_dir, "stop")
        t.join(15)
        self.assertFalse(t.is_alive(), "the owner's stop ends even a hung call")
        st = json.loads((o.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["status"], "STOPPED", st["stop_reason"])
        self.assertNotIn("timeout", st["stop_reason"])

    def test_the_overall_time_budget_pauses_a_long_call_and_resume_continues_the_same_session(self):
        holder = {}
        steps = [{"hang": True, "log": [("WebSearch", "spreading activation")],
                  "partial": [{"path": "notes.md", "content": "research ledger so far"}]}, WRITE_GOOD]
        o, t = self.run_goal([PLAN, OK_REVIEW], steps, budget={"max_minutes": 1}, background=True,
                             on_wait=lambda: holder.setdefault("go", True))
        self.assertTrue(self.wait_for(lambda: holder.get("go")))
        time.sleep(2.5)  # let the session log be observed
        o._elapsed_base = 60.0  # the owner's 1-minute budget is now used up while Claude is still working
        t.join(15)
        self.assertFalse(t.is_alive())
        st = json.loads((o.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["status"], "PAUSED", st["stop_reason"])
        self.assertTrue(st["stop_reason"].startswith("budget_reached: time"), st["stop_reason"])
        first = self.claude_calls()[0]["argv"]
        sid = first[first.index("--session-id") + 1]
        self.assertEqual(st["executor_session_id"], sid, "the interrupted Claude session is kept for the resume")
        self.assertEqual((self.ws / "notes.md").read_text(encoding="utf-8"), "research ledger so far", "completed work is kept")
        self.assertEqual(st["history"][-1]["executor"], "interrupted")
        self.assertIn("BUDGET_REACHED", [m["content"] for m in self.msgs(o) if m["kind"] == "approval_request"][-1])
        self.assertNotEqual(st["status"], "COMPLETE")
        extend_task_budget(self.runs, o.task_id, {"max_minutes": 60})  # the OWNER's decision
        st = orch.Orchestrator(self.cfg_, self.ws, self.runs, *self.agents, out=self.lines.append, task_id=o.task_id).resume()
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        second = self.claude_calls()[1]["argv"]
        self.assertEqual(second[second.index("--resume") + 1], sid, "the same Claude session continues")
        self.assertIn("Continue the SAME assignment", self.sim.prompts[1])
        self.assertIsNone(self.claude_calls()[1]["kw"]["timeout"])

    def test_session_log_unavailable_is_reported_as_unobservable_not_as_failure(self):
        notes, clock = [], [0.0]
        w = cla.SessionWatch(self.base / "nowhere", self.ws, "00000000-0000-4000-8000-000000000000", False,
                             lambda text, notice=False: notes.append((text, notice)), Redactor(), clock=lambda: clock[0])
        for t in (1, 30, 89, 95, 200, 400):
            clock[0] = t
            w.poll()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0][1])
        self.assertIn("NOT observable", notes[0][0])
        self.assertFalse(w.found)


class TestRealProcessRunner(unittest.TestCase):
    def test_no_timeout_lets_a_slow_process_finish_and_cancel_still_works(self):
        r = run_process([sys.executable, "-c", "import time; time.sleep(1.5); print('done')"], cwd=".", env=None, timeout=None)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "done"))
        lines = []
        r = run_process([sys.executable, "-c", "import time; print('a', flush=True); time.sleep(1.2); print('b')"], cwd=".", env=None,
                        timeout=None, on_stdout_line=lines.append)
        self.assertEqual(lines, ["a", "b"])
        t0 = time.time()
        with self.assertRaises(ProcessCancelled):
            run_process([sys.executable, "-c", "import time; time.sleep(60)"], cwd=".", env=None, timeout=None,
                        cancel_check=lambda: time.time() - t0 > 0.5)
        self.assertLess(time.time() - t0, 15)


class TestResearchRubricComesFromTheTask(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_a_non_financial_task_gets_no_trading_template(self):
        self.assertFalse(gm.is_financial(MEMORY_TASK))
        for text in (gm.planner_research_note(MEMORY_TASK), gm.research_rules(MEMORY_TASK), gm.reviewer_research_note(MEMORY_TASK)):
            for banned in ("Out-of-sample", "out-of-sample", "Costs and net performance", "Historical evidence", "profitable edge"):
                self.assertNotIn(banned, text)
        self.assertIn("Derive EVERY acceptance criterion from the task's own requirements", gm.planner_research_note(MEMORY_TASK))
        self.assertIn("Judge against the TASK's own requirements", gm.reviewer_research_note(MEMORY_TASK))
        fin = "Backtest a BTC-USD momentum trading strategy"
        self.assertIn("out-of-sample", gm.planner_research_note(fin))
        self.assertIn("out-of-sample", gm.reviewer_research_note(fin))

    def test_the_tasks_own_decision_format_and_hebrew_headings_are_accepted(self):
        for verdict in ("GO", "PIVOT", "NO-GO", "INCONCLUSIVE"):
            (self.tmp / gm.RESEARCH_REPORT).write_text(HEBREW_REPORT.format(verdict=verdict), encoding="utf-8")
            ev = gm.research_evidence(self.tmp, MEMORY_TASK)
            self.assertTrue(ev["passed"], ev["output_tail"])
            self.assertEqual(ev["verdict"], verdict)
            self.assertNotIn("Out-of-sample validation", ev["sections"])
        (self.tmp / gm.RESEARCH_REPORT).write_text(HEBREW_REPORT.format(verdict="MAYBE"), encoding="utf-8")
        self.assertIn("not a decision defined by the task", gm.research_evidence(self.tmp, MEMORY_TASK)["output_tail"])
        (self.tmp / gm.RESEARCH_REPORT).write_text(HEBREW_REPORT.format(verdict="GO").replace("(נבדק 2026-10-13)", ""), encoding="utf-8")
        self.assertIn("source without a retrieval date", gm.research_evidence(self.tmp, MEMORY_TASK)["output_tail"])

    def test_the_planner_derives_criteria_from_the_real_task(self):
        cli = ca.CodexCli.__new__(ca.CodexCli)
        prompts = []
        cli.call = lambda role, prompt, schema: prompts.append(prompt) or "{}"
        ca.CodexPlanner(cli).plan({"task": MEMORY_TASK, "kind": "research"})
        self.assertIn(MEMORY_TASK.strip(), prompts[0])
        self.assertNotIn("Out-of-sample", prompts[0])
        self.assertNotIn("Costs and net performance", prompts[0])


class TestResearchRunsEndToEnd(MvpBase):
    def run_research(self, verdict, review):
        step = {"ops": [{"op": "write", "path": gm.RESEARCH_REPORT, "content": HEBREW_REPORT.format(verdict=verdict)}]}
        ad = make_mock_agents({"planner": [ms.plan()], "executor": [step], "reviewer": [review]})
        o = orch.Orchestrator(self.cfg(), self.ws, self.runs, *ad, out=self.lines.append, goal=gm.make_goal("goal", "research"))
        return o.start(MEMORY_TASK)

    def test_a_pivot_decision_completes_and_inconclusive_stays_inconclusive(self):
        st = self.run_research("PIVOT", ms.review("COMPLETE"))
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        self.assertIn("verdict PIVOT", st["stop_reason"])

    def test_inconclusive_is_never_reported_as_success(self):
        st = self.run_research("INCONCLUSIVE", ms.review("COMPLETE"))
        self.assertEqual(st["status"], "INCONCLUSIVE")


class TestGoalCyclesAreNotCapped(AppBase):
    def test_goal_mode_from_the_browser_runs_past_the_legacy_three_cycles(self):
        from test_mvp import cx
        again = [cx(ms.review("CONTINUE", findings=["more"], next_instruction=f"Pass {i}: extend NOTES{i}.md.", question=""))
                 for i in range(4)]
        self.fr.execs[:] = [PLAN, *again, OK_REVIEW]  # distinct instructions and real progress: not a stall
        self.sim.steps[:] = [{"ops": WRITE_GOOD["ops"] + [{"op": "write", "path": f"NOTES{i}.md", "content": f"pass {i}"}]}
                             for i in range(5)]
        prep, tid = self.start_task(settings={"mode": "goal", "kind": "code", "max_cycles": 3})
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual((s["status"], s["cycle"]), ("COMPLETE", 5), s["stop_reason"])
        self.assertEqual({c["kw"]["timeout"] for c in self.claude_calls()}, {None})
        src = (Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('S.status.mode !== "goal" ? " מתוך "', src, "goal mode shows 'סבב N', never 'סבב N מתוך 3'")
        self.assertIn("אין מגבלת זמן לקריאה בודדת ואין מגבלת סבבים", src)


if __name__ == "__main__":
    unittest.main()
