"""State persistence reliability: Windows refuses os.replace while another handle (the browser viewer) has state.json open
(captured on this machine: PermissionError [WinError 5] Access is denied). Bounded retries with unique temp files, the actual
error persisted and shown when saving still fails, no orphaned agent process when monitoring fails, and owner budgets that
an interrupted/resumed run can never exceed. Mock agents / temp files only."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import ms, orch  # noqa: F401
from test_goal import GoalBase, bad, cont

import store  # noqa: E402
import web_viewer as wv  # noqa: E402
from safety import Redactor, run_process  # noqa: E402

GOOD = ms.GOOD


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.p = self.d / "state.json"
        self.real_replace = os.replace

    def leftovers(self):
        return [x.name for x in self.d.iterdir() if x.name != "state.json"]

    def test_a_transient_lock_is_retried_and_the_exact_data_is_saved(self):
        n = {"fails": 3}

        def flaky(src, dst):
            if n["fails"]:
                n["fails"] -= 1
                raise PermissionError(13, "Access is denied")
            return self.real_replace(src, dst)
        store.os.replace = flaky
        self.addCleanup(setattr, store.os, "replace", self.real_replace)
        store.atomic_write_json(self.p, {"v": 1, "text": "שלום"})
        self.assertEqual(json.loads(self.p.read_text(encoding="utf-8")), {"v": 1, "text": "שלום"})
        self.assertEqual(self.leftovers(), [], "no temp files are left behind")

    def test_a_lasting_failure_raises_the_original_error_and_keeps_the_previous_state(self):
        store.atomic_write_json(self.p, {"v": "old"})

        def denied(src, dst):
            raise PermissionError(13, "Access is denied")
        store.os.replace, old = denied, store.REPLACE_RETRY_SEC
        store.REPLACE_RETRY_SEC = 0.3
        self.addCleanup(setattr, store.os, "replace", self.real_replace)
        self.addCleanup(setattr, store, "REPLACE_RETRY_SEC", old)
        t0 = time.monotonic()
        with self.assertRaises(PermissionError):
            store.atomic_write_json(self.p, {"v": "new"})
        self.assertLess(time.monotonic() - t0, 3, "bounded")
        self.assertEqual(json.loads(self.p.read_text(encoding="utf-8")), {"v": "old"}, "never a partial or lost file")
        self.assertEqual(self.leftovers(), [])

    def test_other_errors_are_not_retried(self):
        def missing(src, dst):
            raise FileNotFoundError(2, "gone")
        store.os.replace = missing
        self.addCleanup(setattr, store.os, "replace", self.real_replace)
        t0 = time.monotonic()
        with self.assertRaises(FileNotFoundError):
            store.atomic_write_json(self.p, {"v": 1})
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_real_concurrent_readers_and_writers_never_lose_a_save(self):
        """The situation of a running task watched in the browser, much harsher than reality (3 readers, 2 writers)."""
        big = {"task": "x" * 40_000, "history": [{"i": i, "f": ["y" * 300] * 5} for i in range(40)]}
        store.atomic_write_json(self.p, big)
        stop, errors = threading.Event(), []

        def reader():
            while not stop.is_set():
                try:
                    json.loads(self.p.read_text(encoding="utf-8"))
                except ValueError as e:
                    errors.append(f"partial read: {e}")
                except OSError:
                    pass
                time.sleep(0.001)

        def writer(tag):
            for i in range(60):
                try:
                    store.atomic_write_json(self.p, dict(big, writer=tag, n=i))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{type(e).__name__}: {e}")
        readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
        writers = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
        for t in readers + writers:
            t.start()
        for t in writers:
            t.join(120)
        stop.set()
        self.assertEqual(errors, [])
        final = json.loads(self.p.read_text(encoding="utf-8"))
        self.assertEqual(final["n"], 59)
        self.assertEqual(self.leftovers(), [])


class TestTheActualErrorIsKept(GoalBase):
    def test_a_transient_save_failure_does_not_stop_the_run(self):
        real, n = os.replace, {"calls": 0}

        def sometimes(src, dst):
            n["calls"] += 1
            if n["calls"] % 4 == 0:
                raise PermissionError(13, "Access is denied")
            return real(src, dst)
        store.os.replace = sometimes
        self.addCleanup(setattr, store.os, "replace", real)
        o, st = self.goal_run([ms.write_step(bad(0)), ms.write_step(GOOD)], [cont("fix add: return a + b"), ms.review("COMPLETE")])
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])

    def test_a_lasting_save_failure_is_recorded_shown_and_the_run_resumes_cleanly(self):
        real_write, calls = orch.atomic_write_json, {"n": 0}

        def failing_after(path, obj):
            calls["n"] += 1
            if Path(path).name == "state.json" and calls["n"] > 6:
                raise PermissionError(13, "Access is denied", str(path))
            return real_write(path, obj)
        orch.atomic_write_json = failing_after
        try:
            with self.assertRaises(PermissionError):
                self.goal_run([ms.write_step(bad(0))] + [ms.write_step(GOOD)] * 3,  # the interrupted cycle is redone after resume
                              [cont("fix add: return a + b")] + [ms.review("COMPLETE")] * 3, task_id="t-fail")
        finally:
            orch.atomic_write_json = real_write
        run_dir = self.runs / "t-fail"
        rec = json.loads((run_dir / "internal_error.json").read_text(encoding="utf-8"))
        self.assertTrue(rec["error"].startswith("internal_error: PermissionError"), rec)
        self.assertIn("PermissionError", rec["state_save_error"])
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"internal_error"', events)
        status = wv.RunReader(run_dir, Redactor()).status()
        self.assertEqual(status["internal_error"]["error"], rec["error"], "the browser receives the actual error")
        st = orch.Orchestrator(self.cfg(), self.ws, self.runs, *self.agents, out=self.lines.append, task_id="t-fail").resume()
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        self.assertFalse((run_dir / "internal_error.json").exists(), "cleared on resume (kept in the event log)")
        self.assertIn('"internal_error_seen"', (run_dir / "events.jsonl").read_text(encoding="utf-8"))

    def test_any_unexpected_exception_leaves_the_task_paused_with_the_real_error(self):
        planner, executor, reviewer = None, None, None
        o, _ = None, None

        class Boom(Exception):
            pass
        steps = [ms.write_step(GOOD), ms.write_step(GOOD)]  # the interrupted cycle is redone after resume
        from agents import make_mock_agents
        planner, executor, reviewer = make_mock_agents({"planner": [ms.plan()], "executor": steps, "reviewer": [ms.review("COMPLETE")]})
        orig = reviewer.review
        reviewer.review = lambda ctx: (_ for _ in ()).throw(Boom("unexpected bug in a component"))
        o = orch.Orchestrator(self.cfg(), self.ws, self.runs, planner, executor, reviewer, out=self.lines.append,
                              goal=orch.make_goal("goal", "code") if hasattr(orch, "make_goal") else __import__("goal_mode").make_goal("goal", "code"))
        with self.assertRaises(Boom):
            o.start("implement calc.add")
        st = json.loads(o.state_path.read_text(encoding="utf-8"))
        self.assertEqual(st["status"], "PAUSED")
        self.assertEqual(st["stop_reason"], "internal_error: Boom: unexpected bug in a component")
        reviewer.review = orig
        st2 = orch.Orchestrator(self.cfg(), self.ws, self.runs, planner, executor, reviewer, out=self.lines.append, task_id=o.task_id).resume()
        self.assertEqual(st2["status"], "COMPLETE", st2["stop_reason"])
        html = (Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("הריצה נעצרה בגלל תקלה פנימית במערכת (לא בגלל הסוכנים)", html)


class TestNoOrphanedAgentProcess(unittest.TestCase):
    def test_a_failing_monitor_kills_the_agent_process_tree(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        beat = d / "beat"
        script = d / "agent.py"
        script.write_text(f"import time, pathlib\np = pathlib.Path(r'{beat}')\nwhile True:\n    p.write_text(str(time.time()))\n"
                          "    time.sleep(0.05)\n", encoding="utf-8")
        t0 = time.time()

        def broken_monitor():
            if time.time() - t0 > 1.0:
                raise RuntimeError("viewer/log failure")
            return False
        with self.assertRaises(RuntimeError):
            run_process([sys.executable, str(script)], cwd=str(d), env=None, timeout=None, cancel_check=broken_monitor)
        time.sleep(0.5)
        last = beat.read_text(encoding="utf-8")
        time.sleep(0.8)
        self.assertEqual(beat.read_text(encoding="utf-8"), last, "the agent process is no longer running")


class TestBudgetAfterAnInterruption(GoalBase):
    def test_an_interrupted_run_over_its_budget_stays_paused_until_the_owner_extends_it(self):
        o, st = self.goal_run([ms.write_step(bad(0)), ms.write_step(GOOD)], [cont("fix add: return a + b"), ms.review("COMPLETE")],
                              {"max_minutes": 1, "max_claude_calls": 1})
        self.assertEqual(st["status"], "PAUSED")
        s = json.loads(o.state_path.read_text(encoding="utf-8"))
        s.update(status="RUNNING", stop_reason=None, elapsed_sec=75.0)  # as if the process died while running past the budget
        store.atomic_write_json(o.state_path, s)
        calls_before, executor_calls = dict(s["calls"]), len(self.agents[1].calls)
        st2 = self.resume(o)
        self.assertEqual(st2["status"], "PAUSED")
        self.assertTrue(st2["stop_reason"].startswith("budget_reached: time 1/1 min"), st2["stop_reason"])
        self.assertEqual((st2["calls"], st2["budget"]["max_minutes"]), (calls_before, 1), "no call, no silent extension")
        self.assertEqual(len(self.agents[1].calls), executor_calls)


if __name__ == "__main__":
    unittest.main()
