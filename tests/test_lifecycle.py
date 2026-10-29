"""Lifecycle tests: completion, recovery, rejection, limits, errors, approval, resume, CLI."""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import PY, UNITTEST_CMD, Base, ms, orch  # noqa: F401  (sys.path set in helpers)

import store  # noqa: E402
from agents import make_mock_agents  # noqa: E402

GOOD, BAD = ms.GOOD, ms.BAD
PLAN = ms.plan()


def scen(executor, reviewer, planner=None):
    return {"planner": [PLAN] if planner is None else planner, "executor": executor, "reviewer": reviewer}


class TestCompletion(Base):
    def test_successful_complete_task_with_evidence(self):
        o, (pl, ex, rv), st = self.run_task(ms.success_scenario())
        self.assertEqual((st["status"], st["cycle"]), ("COMPLETE", 1))
        self.assertTrue((self.ws / "calc.py").exists())
        h = st["history"][0]
        self.assertEqual((h["verification"], h["tests_ran"], h["reviewer"]), ("passed", 2, "COMPLETE"))
        ev = rv.calls[0]["evidence"]  # what the reviewer actually received
        self.assertEqual(ev["diff"]["added"], ["calc.py"])
        self.assertTrue(ev["verification"]["passed"])
        self.assertEqual(json.loads(o.state_path.read_text(encoding="utf-8"))["status"], "COMPLETE")
        self.assertEqual([p.name for p in o.run_dir.glob("*.tmp*")], [])
        self.assertTrue(any("[cycle 1/8]" in x for x in self.lines))

    def test_executor_process_failure_ends_the_run_without_repeating_it(self):
        _, (_, ex, rv), st = self.run_task(scen([{"error": "process"}, ms.write_step(GOOD)], [ms.review("COMPLETE")]))
        self.assertEqual((st["status"], st["cycle"]), ("FAILED", 1))
        self.assertTrue(st["stop_reason"].startswith("executor_error"))
        self.assertEqual(st["history"][0]["executor"], "error:process")
        self.assertEqual((len(ex.calls), len(rv.calls)), (1, 0), "no second executor call, no review of a failed step")

    def test_reviewer_rejection_leads_to_corrective_iteration(self):
        o, (_, ex, rv), st = self.run_task(ms.rejection_scenario())
        self.assertEqual((st["status"], st["cycle"]), ("COMPLETE", 2))
        self.assertEqual(rv.calls[0]["evidence"]["verification"]["status"], "failed")
        self.assertIn("a + b", ex.calls[1]["instruction"])
        self.assertEqual(st["history"][0]["decision"], "CONTINUE")

    def test_session_id_is_preserved_between_cycles(self):
        _, (_, ex, _), st = self.run_task(ms.rejection_scenario())
        self.assertIsNone(ex.calls[0]["session_id"])
        self.assertEqual(ex.calls[1]["session_id"], "mock-session-1")


class TestCompletionGate(Base):
    def test_complete_rejected_when_verification_fails(self):
        o, _, st = self.run_task(scen([ms.write_step(BAD)], [ms.review("COMPLETE")]), {"max_cycles": 1})
        self.assertNotEqual(st["status"], "COMPLETE")
        self.assertEqual(st["stop_reason"], "max_cycles_reached")
        self.assertEqual(st["history"][0]["reviewer"], "COMPLETE")
        self.assertEqual(st["history"][0]["decision"], "CONTINUE")
        self.assertIn("completion_rejected", self.event_names(o))

    def test_complete_rejected_when_too_few_tests_ran(self):
        _, _, st = self.run_task(scen([ms.write_step(GOOD)], [ms.review("COMPLETE")]),
                                 {"max_cycles": 1, "min_tests_ran": 5})
        self.assertNotEqual(st["status"], "COMPLETE")
        self.assertEqual(st["history"][0]["verification"], "no_test_evidence")

    def test_complete_rejected_without_any_file_change(self):
        (self.ws / "calc.py").write_text(GOOD, encoding="utf-8")  # already solved before the task
        s = scen([{"ops": []}], [ms.review("COMPLETE")])
        _, _, st = self.run_task(s, {"max_cycles": 1})
        self.assertNotEqual(st["status"], "COMPLETE")
        # same facts, but the project owner allows a no-change completion -> accepted
        ws2 = self.new_ws("ws2")
        (ws2 / "calc.py").write_text(GOOD, encoding="utf-8")
        o2, _ = self.make(s, {"require_changes": False}, ws=ws2)
        self.assertEqual(o2.start("t")["status"], "COMPLETE")

    def test_verification_timeout_rejects_completion(self):
        slow = [PY, "-c", "import time; time.sleep(30)"]
        t0 = time.time()
        o, _, st = self.run_task(scen([ms.write_step(GOOD)], [ms.review("COMPLETE")]), {
            "verify_command": slow, "command_allowlist": [["python", "-c", slow[2]]],
            "verification_timeout_sec": 1, "max_cycles": 1, "min_tests_ran": 0})
        self.assertLess(time.time() - t0, 20)
        self.assertEqual(st["history"][0]["verification"], "timeout")
        self.assertNotEqual(st["status"], "COMPLETE")

    def test_stop_cancels_running_verification(self):
        slow = [PY, "-c", "import time; time.sleep(60)"]
        o, _ = self.make(scen([ms.write_step(GOOD)], [ms.review("COMPLETE")]), {
            "verify_command": slow, "command_allowlist": [["python", "-c", slow[2]]],
            "verification_timeout_sec": 60, "min_tests_ran": 0})
        threading.Timer(2.0, lambda: store.write_control(o.run_dir, "stop")).start()
        t0 = time.time()
        st = o.start("t")
        self.assertLess(time.time() - t0, 25)
        self.assertEqual((st["status"], st["stop_reason"]), ("STOPPED", "user_stop"))


class TestLimits(Base):
    def test_max_cycle_limit(self):
        steps = [ms.write_step(BAD + f"# attempt {i}\n") for i in range(5)]
        revs = [ms.review("CONTINUE", next_instruction="still failing: return a + b in add()")] * 5
        o, (_, ex, _), st = self.run_task(scen(steps, revs), {"max_cycles": 3})
        self.assertEqual((st["status"], st["stop_reason"], st["cycle"]), ("NEEDS_REVISION", "max_cycles_reached", 3))
        self.assertEqual(len(ex.calls), 3)

    def test_repeated_failure_without_progress_stops(self):
        _, (_, ex, _), st = self.run_task(scen([{"error": "malformed"}] * 5, []))
        self.assertEqual(st["status"], "FAILED")
        _, (_, ex2, rv2), st2 = self.run_task(scen([{"error": "timeout"}] * 5, [ms.review("COMPLETE")]), ws=self.new_ws("w2"))
        self.assertEqual((st2["status"], st2["cycle"]), ("FAILED", 1))
        self.assertTrue(st2["stop_reason"].startswith("executor_error"))
        self.assertEqual((len(ex2.calls), len(rv2.calls)), (1, 0), "a timeout consumes no further cycle and no further call")

    def test_reviewer_failure_never_starts_another_cycle(self):
        _, (_, ex, rv), st = self.run_task(scen([ms.write_step(GOOD)] * 3, [{"error": "timeout"}, ms.review("COMPLETE")]))
        self.assertEqual((st["status"], st["cycle"]), ("FAILED", 1))
        self.assertTrue(st["stop_reason"].startswith("reviewer_error (timeout)"))
        self.assertEqual((len(ex.calls), len(rv.calls)), (1, 1))

    def test_unchanged_workspace_and_same_failure_stops(self):
        revs = [ms.review("CONTINUE", next_instruction="tests still fail, implement add()")] * 6
        _, _, st = self.run_task(scen([{"ops": []}] * 6, revs))
        self.assertEqual(st["status"], "FAILED")
        self.assertTrue(st["stop_reason"].startswith("no_progress"))
        self.assertLess(st["cycle"], 8)

    def test_executor_turn_limit_enforced(self):
        _, _, st = self.run_task(scen([ms.write_step(GOOD, turns=99)], []), {"max_cycles": 1})
        self.assertEqual(st["history"][0]["executor"], "error:process")


class TestErrors(Base):
    def assertFatal(self, st, reason):
        self.assertEqual(st["status"], "BLOCKED" if reason in ("auth_error", "quota_exceeded", "configuration_error") else "FAILED")
        self.assertTrue(st["stop_reason"].startswith(reason), st["stop_reason"])

    def test_malformed_outputs_stop_the_task(self):
        for name, s in {
            "executor text": scen([{"raw": "I fixed it!"}], []),
            "executor bad session id": scen([ms.write_step(GOOD, session_id="x; rm -rf /")], []),
            "reviewer bad verdict": scen([ms.write_step(GOOD)], [{"raw": json.dumps({"verdict": "DONE"})}]),
            "reviewer vague continue": scen([ms.write_step(GOOD)], [ms.review("CONTINUE", next_instruction="fix")]),
            "reviewer not an object": scen([ms.write_step(GOOD)], [{"raw": "[1,2]"}]),
            "planner broken json": scen([], [], planner=[{"raw": "{"}] * 3),  # 1 answer + 2 correction requests
        }.items():
            with self.subTest(name):
                ws = self.new_ws("ws_" + name.replace(" ", "_"))
                o, _ = self.make(s, ws=ws)
                self.assertFatal(o.start("t"), "malformed_output")

    def test_authentication_error_stops_immediately(self):
        _, (_, ex, _), st = self.run_task(scen([{"error": "auth"}] * 3, []))
        self.assertFatal(st, "auth_error")
        self.assertEqual((st["cycle"], len(ex.calls)), (1, 1))
        ws2 = self.new_ws("ws2")
        o2, _ = self.make(scen([], [], planner=[{"error": "auth"}]), ws=ws2)
        self.assertFatal(o2.start("t"), "auth_error")

    def test_subscription_quota_error_stops_immediately(self):
        _, _, st = self.run_task(scen([ms.write_step(GOOD)], [{"error": "quota"}]))
        self.assertFatal(st, "quota_exceeded")
        ws2 = self.new_ws("ws2")
        o2, _ = self.make(scen([{"error": "quota"}], []), ws=ws2)
        self.assertFatal(o2.start("t"), "quota_exceeded")

    def test_reviewer_declares_failure_and_human_request(self):
        _, _, st = self.run_task(scen([ms.write_step(GOOD)], [ms.review("FAILED", findings=["blocked"])]))
        self.assertFatal(st, "reviewer_declared_failure")
        ws2 = self.new_ws("ws2")
        o2, _ = self.make(scen([ms.write_step(GOOD)], [ms.review("HUMAN_REQUIRED", question="Which API?")]), ws=ws2)
        st2 = o2.start("t")
        self.assertEqual((st2["status"], st2["pending_question"]), ("HUMAN_REQUIRED", "Which API?"))


class TestApproval(Base):
    def test_delete_needs_approval_then_resume(self):
        (self.ws / "old.txt").write_text("legacy", encoding="utf-8")
        delete = {"ops": [{"op": "delete", "path": "old.txt"}, {"op": "write", "path": "calc.py", "content": GOOD}]}
        o1, _ = self.make(scen([delete], []))
        st = o1.start("t")
        self.assertEqual(st["status"], "HUMAN_REQUIRED")
        self.assertTrue(st["stop_reason"].startswith("approval_required:a1"))
        self.assertTrue((self.ws / "old.txt").exists(), "nothing may be deleted before approval")
        # an active task blocks a second task on the same workspace
        o_other, _ = self.make(ms.success_scenario())
        with self.assertRaises(store.ActiveTaskExists):
            o_other.start("other")
        # resume refuses while the approval is pending
        o2 = orch.Orchestrator(self.cfg(), self.ws, self.runs, *make_mock_agents(scen([delete], [ms.review("COMPLETE")], [])),
                               out=self.lines.append, task_id=o1.task_id)
        with self.assertRaises(orch.OrchestratorError):
            o2.resume()
        orch.resolve_approval(self.runs, o1.task_id, "a1", True)
        st = o2.resume()
        self.assertEqual(st["status"], "COMPLETE")
        self.assertFalse((self.ws / "old.txt").exists())
        self.assertIn("approval_resolved", self.event_names(o2))

    def test_denied_approval_fails_the_task_and_keeps_the_file(self):
        (self.ws / "old.txt").write_text("legacy", encoding="utf-8")
        o, _ = self.make(scen([{"ops": [{"op": "delete", "path": "old.txt"}]}], []))
        o.start("t")
        st = orch.resolve_approval(self.runs, o.task_id, "a1", False)
        self.assertEqual((st["status"], st["stop_reason"]), ("FAILED", "approval_denied"))
        self.assertTrue((self.ws / "old.txt").exists())


class TestPersistence(Base):
    def test_pause_persist_resume_in_a_fresh_instance(self):
        first = scen([ms.write_step(BAD, session_id="sess-42")],
                     [ms.review("CONTINUE", next_instruction="add() subtracts; it must return a + b")])
        o1, _ = self.make(first, after_cycle=lambda o, c: store.write_control(o.run_dir, "pause") if c == 1 else None)
        st = o1.start("t")
        self.assertEqual((st["status"], st["cycle"]), ("PAUSED", 1))
        disk = json.loads(o1.state_path.read_text(encoding="utf-8"))
        self.assertEqual(disk["executor_session_id"], "sess-42")
        self.assertTrue((o1.run_dir / "baseline.json").exists())
        # brand-new objects, as if the process had been restarted
        planner2, executor2, reviewer2 = make_mock_agents(scen([ms.write_step(GOOD)], [ms.review("COMPLETE")], []))
        o2 = orch.Orchestrator(self.cfg(), self.ws, self.runs, planner2, executor2, reviewer2,
                               out=self.lines.append, task_id=o1.task_id)
        st = o2.resume()
        self.assertEqual((st["status"], st["cycle"]), ("COMPLETE", 2))
        self.assertEqual(planner2.calls, [], "plan must not be requested again")
        self.assertIn("a + b", executor2.calls[0]["instruction"])
        self.assertEqual(executor2.calls[0]["session_id"], "sess-42")
        seqs = [e["seq"] for e in self.events(o2)]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        with self.assertRaises(orch.OrchestratorError):  # terminal states cannot be resumed
            orch.Orchestrator(self.cfg(), self.ws, self.runs, *make_mock_agents({}), task_id=o1.task_id).resume()

    def test_atomic_write_keeps_old_state_on_failure(self):
        p = self.base / "x.json"
        store.atomic_write_json(p, {"a": 1})
        with self.assertRaises(TypeError):
            store.atomic_write_json(p, {"a": {1, 2}})
        self.assertEqual(json.loads(p.read_text()), {"a": 1})
        self.assertEqual([x.name for x in self.base.glob("x.json.tmp*")], [])


class TestCli(Base):
    def setUp(self):
        super().setUp()
        self.cfgfile = self.base / "config.json"
        self.cfgfile.write_text(json.dumps({"runs_dir": str(self.runs), "verify_command": UNITTEST_CMD}), encoding="utf-8")
        self.scn = self.base / "scenario.json"
        self.scn.write_text(json.dumps(ms.success_scenario()), encoding="utf-8")

    def cli(self, *args):
        return orch.main(["--config", str(self.cfgfile), *args], out=self.lines.append)

    def test_run_status_resume_and_stop_commands(self):
        self.assertEqual(self.cli("run", "--project", str(self.ws), "--task", "t", "--scenario", str(self.scn)), 0)
        self.cli("status")
        self.assertTrue(any("COMPLETE" in x for x in self.lines))
        self.assertEqual(self.cli("resume"), 2)  # completed tasks cannot be resumed
        ws2 = self.new_ws("ws2")
        (ws2 / "old.txt").write_text("x", encoding="utf-8")
        needs_approval = scen([{"ops": [{"op": "delete", "path": "old.txt"}]}], [])
        self.scn.write_text(json.dumps(needs_approval), encoding="utf-8")
        self.assertEqual(self.cli("run", "--project", str(ws2), "--task", "t", "--scenario", str(self.scn)), 3)
        self.assertEqual(self.cli("stop"), 0)  # HUMAN_REQUIRED -> STOPPED directly
        self.cli("status", "--json")
        self.assertIn('"status": "STOPPED"', self.lines[-1])
        self.assertTrue((ws2 / "old.txt").exists())

    def test_dry_run_has_no_side_effects(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no process allowed")):
            rc = self.cli("run", "--project", str(self.ws), "--task", "t", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertFalse(self.runs.exists())
        self.assertTrue(any("DRY RUN" in x for x in self.lines))

    def test_live_mode_is_refused_and_starts_nothing(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no process allowed")):
            rc = self.cli("run", "--project", str(self.ws), "--task", "t", "--agents", "live")
        self.assertEqual(rc, 2)
        self.assertFalse(self.runs.exists())

    def test_shipped_config_is_valid_and_matches_defaults(self):
        shipped = orch.load_config(orch.HERE / "config.json")
        self.assertEqual(shipped, orch.DEFAULT_CONFIG)

    def test_config_validation(self):
        for bad in ({"max_cycles": 0}, {"max_cycles": "8"}, {"verify_command": "pytest"}, {"live": {"enabled": "yes"}}):
            with self.subTest(bad), self.assertRaises(orch.ConfigError):
                orch.load_config(None, bad)
        self.cfgfile.write_text(json.dumps({"max_cyles": 3}), encoding="utf-8")
        with self.assertRaises(orch.ConfigError):
            orch.load_config(self.cfgfile)


if __name__ == "__main__":
    unittest.main()
