"""Goal-driven execution: long runs, budgets that pause, owner-only extension, stalls, persistence, truthful research.
MOCK agents / simulated CLIs only - nothing here demonstrates real autonomous research success."""
import json
import unittest

from helpers import Base, ms, orch

import claude_adapter as cla  # noqa: E402
import goal_mode as gm  # noqa: E402
from agents import make_mock_agents  # noqa: E402
from config import DEFAULT_CONFIG, OrchestratorError  # noqa: E402
from taskctl import extend_task_budget  # noqa: E402

GOOD = ms.GOOD


def bad(i):  # a different wrong implementation each time -> measurable change, different failure output
    return f"def add(a, b):\n    return a - b + {i}\n"


def cont(text):
    return ms.review("CONTINUE", findings=["tests fail"], next_instruction=text)


REPORT = """# Momentum study
## Hypotheses
Short-term momentum may predict next-hour returns (exploration only).
## Data and method
BTC-USD 1h, 2022-2025, walk-forward.
## Historical evidence
In-sample Sharpe 0.9 before costs.
## Out-of-sample validation
{oos}
## Costs and net performance
{costs}
## Unresolved uncertainty
Regime dependence; small sample.
## Sources
- https://api.exchange.example/candles?product=BTC-USD (retrieved 2026-10-12 21:30 UTC): hourly candles 2022-2025.
## Conclusion
{concl}
Verdict: {verdict}
"""


def report(verdict, oos="2025 holdout: Sharpe 0.2, 410 trades.", costs="0.1% per side: net Sharpe -0.1.", concl="See verdict."):
    return REPORT.format(oos=oos, costs=costs, concl=concl, verdict=verdict)


class GoalBase(Base):
    def goal_run(self, steps, reviews, budget=None, kind="code", cfg_over=None, task="implement calc.add", **kw):
        planner, executor, reviewer = make_mock_agents({"planner": [ms.plan()], "executor": steps, "reviewer": reviews})
        self.agents = (planner, executor, reviewer)
        o = orch.Orchestrator(self.cfg(**(cfg_over or {})), self.ws, self.runs, *self.agents, out=self.lines.append,
                              goal=gm.make_goal("goal", kind, budget), **kw)
        return o, o.start(task)

    def resume(self, o, cfg_over=None):
        return orch.Orchestrator(self.cfg(**(cfg_over or {})), self.ws, self.runs, *self.agents, out=self.lines.append,
                                 task_id=o.task_id).resume()

    def msgs(self, o):
        return [json.loads(x) for x in (o.run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]


class TestGoalDriven(GoalBase):
    def test_keeps_going_past_the_old_cycle_limit_until_verified(self):
        steps = [ms.write_step(bad(i)) for i in range(7)] + [ms.write_step(GOOD)]
        reviews = [cont(f"attempt {i}: add() must return a + b; fix calc.py") for i in range(7)] + [ms.review("COMPLETE")]
        o, st = self.goal_run(steps, reviews, {"checkpoint_cycles": 20})
        self.assertEqual((st["status"], st["cycle"]), ("COMPLETE", 8), st["stop_reason"])
        self.assertEqual(st["calls"], {"codex": 9, "claude": 8})
        self.assertEqual(st["mode"], "goal")

    def test_the_cycle_checkpoint_reports_progress_and_work_continues(self):
        steps = [ms.write_step(bad(i)) for i in range(6)] + [ms.write_step(GOOD)]
        reviews = [cont(f"try {i}: return a + b in calc.py") for i in range(6)] + [ms.review("COMPLETE")]
        o, st = self.goal_run(steps, reviews, {"checkpoint_cycles": 3})
        self.assertEqual((st["status"], st["cycle"]), ("COMPLETE", 7), st["stop_reason"])
        cps = [json.loads(m["content"]) for m in self.msgs(o) if m["kind"] == "checkpoint"]
        self.assertEqual([c["cycles_done"] for c in cps], [3, 6], "a progress report every 3 cycles, never a stop")
        self.assertTrue(all(c["continuing"] and c["progress_cycles_in_window"] == 3 for c in cps))
        self.assertNotIn("approval_request", [m["kind"] for m in self.msgs(o)])

    def test_a_budget_pause_then_owner_extension_resumes_without_resetting_anything(self):
        steps = [ms.write_step(bad(i)) for i in range(5)] + [ms.write_step(GOOD)]
        reviews = [cont(f"try {i}: return a + b in calc.py") for i in range(5)] + [ms.review("COMPLETE")]
        o, st = self.goal_run(steps, reviews, {"max_claude_calls": 3})
        self.assertEqual((st["status"], st["stop_reason"]), ("PAUSED", "budget_reached: claude calls 3/3"))
        self.assertEqual(st["calls"], {"codex": 4, "claude": 3})
        asks = [m for m in self.msgs(o) if m["kind"] == "approval_request"]
        self.assertIn("BUDGET_REACHED: claude calls 3/3", asks[-1]["content"])
        st2 = self.resume(o, {"max_cycles": 1, "max_claude_calls": 50})  # a different config cannot reset or widen the persisted budget
        self.assertEqual((st2["status"], st2["cycle"]), ("PAUSED", 3))
        extend_task_budget(self.runs, o.task_id, {"max_claude_calls": 5})
        st3 = self.resume(o, {"max_cycles": 1})
        self.assertEqual((st3["status"], st3["cycle"]), ("COMPLETE", 6), st3["stop_reason"])
        self.assertEqual(st3["calls"], {"codex": 7, "claude": 6}, "counters continue across pause and resume")
        self.assertIn("BUDGET EXTENDED by the owner: max_claude_calls 3 -> 8", [m["content"] for m in self.msgs(o) if m["kind"] == "approval_decision"][0])

    def test_call_and_time_budgets_pause(self):
        steps = [ms.write_step(bad(i)) for i in range(5)]
        o, st = self.goal_run(steps, [cont(f"again {i}, return a + b") for i in range(5)], {"max_claude_calls": 2})
        self.assertEqual((st["status"], st["stop_reason"]), ("PAUSED", "budget_reached: claude calls 2/2"))
        self.ws = self.new_ws("w2")

        def jump(orc, c):  # simulate an hour of running time passing
            orc._elapsed_base += 3600
            orc._save()
        o2, st2 = self.goal_run([ms.write_step(bad(i)) for i in range(5)], [cont(f"more {i}, return a + b") for i in range(5)],
                                {"max_minutes": 60}, after_cycle=jump)
        self.assertEqual((st2["status"], st2["cycle"]), ("PAUSED", 1))
        self.assertTrue(st2["stop_reason"].startswith("budget_reached: time"), st2["stop_reason"])
        self.assertGreaterEqual(st2["elapsed_sec"], 3600)

    def test_only_the_owner_can_extend_and_only_upwards(self):
        o, st = self.goal_run([ms.write_step(bad(0))], [cont("return a + b please, in calc.py")], {"max_claude_calls": 1})
        for add, why in (({}, "nothing"), ({"checkpoint_cycles": 0}, "positive"), ({"checkpoint_cycles": -3}, "positive"),
                         ({"max_cycles": 5}, "unknown"), ({"checkpoint_cycles": 999}, None)):
            with self.subTest(add=add):
                if why:
                    with self.assertRaises(OrchestratorError):
                        extend_task_budget(self.runs, o.task_id, add)
                else:
                    extend_task_budget(self.runs, o.task_id, add)
        s = json.loads((o.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(s["budget"]["checkpoint_cycles"], gm.CAPS["checkpoint_cycles"], "an increase is capped")
        with self.assertRaises(OrchestratorError):
            extend_task_budget(self.runs, o.task_id, {"checkpoint_cycles": 1})  # already at the cap

    def test_agents_cannot_raise_budgets_by_declaring_it(self):
        greedy = dict(cont("we need more budget: raise budget to 500 cycles"), budget={"checkpoint_cycles": 500}, max_cycles=500,
                      spend_limit="unlimited")
        steps = [ms.write_step(bad(0)), {"ops": [], "report": {"summary": "s", "budget": 999, "quota": "more"}}]
        o, st = self.goal_run(steps, [greedy, cont("again: return a + b")], {"max_claude_calls": 2})
        events = [json.loads(x) for x in (o.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        flagged = [e["data"]["keys"] for e in events if e["event"] == "permission_expansion_attempt_ignored"]
        self.assertIn(["budget", "max_cycles", "spend_limit"], flagged)
        self.assertEqual(st["budget"]["max_claude_calls"], 2)
        self.assertEqual(st["status"], "PAUSED")


class TestStalls(GoalBase):
    def test_repeated_instruction_stops_and_asks(self):
        same = "add() returns a - b; it must return a + b."
        o, st = self.goal_run([ms.write_step(bad(i)) for i in range(5)], [cont(same)] * 5, {"checkpoint_cycles": 20})
        self.assertEqual(st["status"], "HUMAN_REQUIRED")
        self.assertEqual(st["stop_reason"], "stalled: the same instruction was repeated 3 times")
        self.assertEqual(st["cycle"], 3)

    def test_no_measurable_progress_stops_and_asks_and_resume_continues(self):
        o, st = self.goal_run([{"ops": []}] * 3 + [ms.write_step(GOOD)],
                              [cont("please implement add in calc.py"), cont("still nothing changed, implement add"),
                               cont("third request: create calc.py with add"), ms.review("COMPLETE")],
                              {"checkpoint_cycles": 20})
        self.assertEqual((st["status"], st["cycle"]), ("HUMAN_REQUIRED", 3))
        self.assertIn(st["stop_reason"], ("stalled: no measurable progress in 2 consecutive cycles",
                                          "stalled: the same verification failure repeated 3 times"))
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        self.assertEqual(st2["budget"]["checkpoint_cycles"], 20, "resuming after a stall does not touch the budget")

    def test_fixed_mode_is_unchanged(self):
        planner, executor, reviewer = make_mock_agents({"planner": [ms.plan()], "executor": [ms.write_step(bad(i)) for i in range(3)],
                                                        "reviewer": [cont(f"x{i} return a + b") for i in range(3)]})
        st = orch.Orchestrator(self.cfg(max_cycles=3), self.ws, self.runs, planner, executor, reviewer, out=self.lines.append).start("t")
        self.assertEqual((st["status"], st["stop_reason"], st["mode"], st["budget"]), ("NEEDS_REVISION", "max_cycles_reached", "fixed", None))


class TestPersistence(GoalBase):
    def test_state_survives_a_process_restart(self):
        steps = [ms.write_step(bad(0)), ms.write_step(GOOD)]
        o, st = self.goal_run(steps, [cont("fix add: return a + b"), ms.review("COMPLETE")], {"max_claude_calls": 1})
        self.assertEqual(st["status"], "PAUSED")
        s = json.loads((o.run_dir / "state.json").read_text(encoding="utf-8"))  # what a new process sees
        self.assertEqual((s["mode"], s["acceptance_criteria"], len(s["history"])), ("goal", ms.plan()["acceptance_criteria"], 1))
        self.assertEqual(s["instruction"], "fix add: return a + b", "the current strategy is kept")
        self.assertGreater(s["elapsed_sec"], 0)
        extend_task_budget(self.runs, o.task_id, {"max_claude_calls": 1})
        st2 = self.resume(o)
        self.assertEqual((st2["status"], st2["cycle"]), ("COMPLETE", 2))
        self.assertGreaterEqual(st2["elapsed_sec"], s["elapsed_sec"], "running time accumulates across processes")


class TestResearch(GoalBase):
    def write(self, text):
        return {"ops": [{"op": "write", "path": gm.RESEARCH_REPORT, "content": text}]}

    def test_inconclusive_research_is_reported_as_inconclusive(self):
        o, st = self.goal_run([self.write(report("INCONCLUSIVE", concl="Net of costs the effect disappears out of sample."))],
                              [ms.review("COMPLETE")], kind="research")
        self.assertEqual(st["status"], "INCONCLUSIVE")
        self.assertEqual(orch.EXIT["INCONCLUSIVE"], 8)
        ver = json.loads([m for m in self.msgs(o) if m["kind"] == "verification"][0]["content"])["verification"]
        self.assertEqual((ver["mode"], ver["executed"], ver["verdict"]), ("research", False, "INCONCLUSIVE"))

    def test_an_inconclusive_hypothesis_leads_to_the_next_approach_not_to_the_end(self):
        first = report("INCONCLUSIVE", concl="Momentum: no out-of-sample edge. Remaining approach: mean reversion on 4h bars.")
        second = report("INCONCLUSIVE", concl="Mean reversion: also no net edge. No further justified approach remains in scope.")
        o, st = self.goal_run([self.write(first), self.write(second)],
                              [cont("Momentum was inconclusive; test mean reversion on 4h bars next."), ms.review("COMPLETE")], kind="research")
        self.assertEqual((st["status"], st["cycle"]), ("INCONCLUSIVE", 2), "the first INCONCLUSIVE report did not end the goal")
        self.assertEqual(st["history"][0]["decision"], "CONTINUE")

    def test_a_profitable_edge_needs_out_of_sample_and_cost_evidence(self):
        unproven = report("SUPPORTED", oos="Not performed.", costs="Not applicable.")
        proven = report("SUPPORTED", oos="2025 holdout: Sharpe 1.1 over 420 trades.", costs="0.1% per side: net Sharpe 0.8.")
        o, st = self.goal_run([self.write(unproven), self.write(proven)],
                              [ms.review("COMPLETE"), ms.review("COMPLETE")], kind="research",
                              task="Backtest a BTC-USD momentum trading strategy and report whether it has a net edge.")
        notices = [m["content"] for m in self.msgs(o) if m["kind"] == "notice"]
        self.assertTrue(any("research_failed" in n for n in notices), notices)
        self.assertEqual((st["status"], st["cycle"]), ("COMPLETE", 2))
        self.assertIn("not for truth", st["stop_reason"])

    def test_missing_report_or_sections_never_complete(self):
        ev = gm.research_evidence(self.ws)
        self.assertEqual((ev["status"], ev["passed"]), ("research_missing", False))
        (self.ws / gm.RESEARCH_REPORT).write_text("## Hypotheses\nx\nVerdict: SUPPORTED\n", encoding="utf-8")
        ev = gm.research_evidence(self.ws)
        self.assertFalse(ev["passed"])
        self.assertIn("no sources section", ev["output_tail"])
        (self.ws / gm.RESEARCH_REPORT).write_text(report("SUPPORTED", oos="Not performed.", costs="Not applicable."), encoding="utf-8")
        self.assertTrue(gm.research_evidence(self.ws, "Is caching worth it for our API?")["passed"], "no trading rules for a non-trading task")
        ev = gm.research_evidence(self.ws, "Backtest a momentum trading strategy")
        self.assertFalse(ev["passed"])
        self.assertIn("verdict SUPPORTED but 'Out-of-sample validation' contains no measured figures", ev["output_tail"])

    def test_codex_plans_and_reviews_research_as_research(self):
        from test_codex_adapter import FakeRunner
        import codex_adapter as ca
        fr = FakeRunner([])
        cli = ca.CodexCli(self.cfg(), runner=fr)
        planner, reviewer = ca.CodexPlanner(cli), ca.CodexReviewer(cli)
        prompts = []
        cli.call = lambda role, prompt, schema: prompts.append(prompt) or "{}"
        planner.plan({"task": "Is momentum profitable?", "kind": "research"})
        reviewer.review({"task": "t", "cycle": 1, "acceptance_criteria": [], "executor_report_UNTRUSTED": {}, "report_discrepancy": {},
                         "evidence": {"verification": gm.research_evidence(self.ws)}})
        self.assertIn("TASK TYPE: RESEARCH", prompts[0]); self.assertIn("INCONCLUSIVE verdict is an acceptable", prompts[0])
        self.assertIn("REVIEW MODE: research", prompts[1]); self.assertIn("prefer INCONCLUSIVE over overclaiming", prompts[1])
        self.assertIn("is NOT the end of the research", prompts[1]); self.assertIn("answer CONTINUE with that next approach", prompts[1])

    def test_research_sources_need_retrieval_dates(self):
        (self.ws / gm.RESEARCH_REPORT).write_text(report("INCONCLUSIVE").replace("(retrieved 2026-10-12 21:30 UTC)", "(retrieved recently)"),
                                                  encoding="utf-8")
        ev = gm.research_evidence(self.ws)
        self.assertFalse(ev["passed"])
        self.assertIn("source without a retrieval date", ev["output_tail"])

    def test_web_research_is_on_with_safeguards(self):
        cfg = orch.load_config(None)
        cl = cfg["claude"]
        self.assertTrue(cfg["codex"]["web_search"])
        self.assertTrue({"WebSearch", "WebFetch"} <= set(cl["tools"]) & set(cl["allowed_tools"]))
        for rule in ("Read(**/.env)", "Read(**/*.pem)", "Read(**/auth.json)", "Bash(curl *)", "Bash(wget *)", "Bash(pip *)", "Bash(rm *)"):
            self.assertIn(rule, cl["disallowed_tools"], "credential files stay unreadable; no download/install/destructive commands")
        exe = self.base / "claude.cmd"
        exe.write_text("x", encoding="utf-8")
        text = cla.ClaudeCli(self.cfg(claude={**cl, "executable": str(exe)})).render_prompt(
            {"task": "t", "cycle": 1, "instruction": "i", "kind": "research"})
        for needed in ("WebSearch and WebFetch", "Never send secrets", "never sign up, log in, pay, purchase, trade", '"sources"',
                       "retrieval date"):
            self.assertIn(needed, text)

    def test_codex_web_searches_are_recorded_in_the_transcript_and_stay_read_only(self):
        from test_codex_adapter import FakeRunner, base_events
        import codex_adapter as ca
        from config import DEFAULT_CONFIG as D
        home = self.base / "cxhome"; home.mkdir()
        (home / "config.toml").write_text('model_reasoning_effort = "low"\nforced_login_method = "chatgpt"\nsandbox_mode = "read-only"\n'
                                          'approval_policy = "never"\n\n[skills]\ninclude_instructions = false\n\n[features]\nhooks = false\n', encoding="utf-8")
        proj = self.base / "proj"
        exe = proj / "toolchain" / "codex" / "node_modules" / ".bin" / "codex.cmd"
        exe.parent.mkdir(parents=True); exe.write_text("x", encoding="utf-8")
        cfg = self.cfg(codex=dict(D["codex"], home=str(home), executable=str(exe)))
        plan_json = json.dumps(ms.plan())
        ev = base_events(plan_json)
        ev.insert(2, {"type": "item.completed", "item": {"id": "w1", "type": "web_search", "query": "fast fourier transform python"}})
        bad_ev = base_events(plan_json)
        bad_ev.insert(2, {"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "rm -rf x"}})
        cli = ca.CodexCli(cfg, base_dir=proj, runner=FakeRunner([{"final": plan_json, "events": ev}, {"final": plan_json, "events": bad_ev}]))
        planner = ca.CodexPlanner(cli)
        planner.plan({"task": "t"})
        self.assertEqual(cli.last_meta["web_searches"][0]["query"], "fast fourier transform python",
                         "a search query is recorded (and the word 'fast' in it is not mistaken for a speed tier)")
        with self.assertRaises(Exception):
            planner.plan({"task": "t"})  # a command or file change is still refused: Codex stays read-only
        _, executor, reviewer = make_mock_agents({"executor": [ms.write_step(GOOD)], "reviewer": [ms.review("COMPLETE")]})
        cli.runner = FakeRunner([{"final": plan_json, "events": ev}])
        o = orch.Orchestrator(cfg, self.ws, self.runs, ca.CodexPlanner(cli), executor, reviewer, out=self.lines.append)
        o.start("t")
        plan_msg = [json.loads(x) for x in (o.run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if '"plan"' in x][0]
        self.assertEqual(plan_msg["meta"]["web_searches"][0]["query"], "fast fourier transform python")
        self.assertIn("recorded_at", plan_msg["meta"]["web_searches"][0])

    def test_claude_gets_the_research_rules(self):
        exe = self.base / "claude.cmd"
        exe.write_text("x", encoding="utf-8")
        cli = cla.ClaudeCli(self.cfg(claude={**DEFAULT_CONFIG["claude"], "executable": str(exe)}))
        text = cli.render_prompt({"task": "t", "cycle": 1, "instruction": "i", "kind": "research"})
        for needed in ("THIS IS A RESEARCH TASK", gm.RESEARCH_REPORT, "exactly as the TASK specifies", "INCONCLUSIVE", "Do not trade"):
            self.assertIn(needed, text)
        self.assertNotIn("Out-of-sample", text, "no trading template for a non-trading task")
        text = cli.render_prompt({"task": "Backtest a trading strategy", "cycle": 1, "instruction": "i", "kind": "research"})
        self.assertIn("Out-of-sample validation", text)


if __name__ == "__main__":
    unittest.main()
