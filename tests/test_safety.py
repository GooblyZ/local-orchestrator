"""Safety tests: redaction, env scrubbing, path/command policy, post-hoc detection,
process timeout/cancellation, locks, evidence. All fake secrets are built at runtime."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import PY, ROOT, UNITTEST_CMD, Base, ms, orch  # noqa: F401

import safety  # noqa: E402
import store  # noqa: E402
from agents import make_mock_agents  # noqa: E402

GOOD = ms.GOOD
PLAN = ms.plan()


def scen(executor, reviewer, planner=None):
    return {"planner": [PLAN] if planner is None else planner, "executor": executor, "reviewer": reviewer}


FAKE_ANT = "sk-" + "ant-api03-" + "A1b2C3d4" * 4
FAKE_OAI = "OPENAI_API_KEY=" + "abc123def456ghi789"
FAKE_BEARER = "Bearer " + "q" * 32
FAKE_ENV_VALUE = "zz-" + "private-env-value-" + "98765"
FAKE_JWT = "eyJ" + "hbGciOiJIUzI1" + "." + "eyJzdWIiOiIxMjM0" + "." + "SflKxwRJSMeKKF2QT4"


class TestRedaction(Base):
    def test_secrets_never_reach_logs_state_or_terminal(self):
        leak = f"{FAKE_ANT} {FAKE_OAI} {FAKE_BEARER} {FAKE_ENV_VALUE} {FAKE_JWT}"
        s = scen([ms.write_step(GOOD, summary=leak, remaining_issues=[leak])],
                 [ms.review("COMPLETE", findings=[leak])], planner=[ms.plan(instruction="do it " + leak)])
        with mock.patch.dict(os.environ, {"MY_SERVICE_TOKEN": FAKE_ENV_VALUE}):
            o, _ = self.make(s)
            st = o.start("task mentioning " + leak)
        self.assertEqual(st["status"], "COMPLETE")
        blob = "\n".join([(o.run_dir / "events.jsonl").read_text(encoding="utf-8"),
                          o.state_path.read_text(encoding="utf-8"), "\n".join(self.lines)])
        for secret in (FAKE_ANT, "abc123def456ghi789", "q" * 32, FAKE_ENV_VALUE, FAKE_JWT):
            self.assertNotIn(secret, blob)
        self.assertIn("[REDACTED]", blob)

    def test_redactor_unit(self):
        r = safety.Redactor({})
        self.assertNotIn("hunter2hunter2", r.text('{"password": "hunter2hunter2"}'))
        self.assertNotIn("zzzzzzzz", r.obj({"api_key": "zzzzzzzz", "n": 1})["api_key"])
        self.assertEqual(r.obj({"n": 1, "l": ["ok"]}), {"n": 1, "l": ["ok"]})


class TestEnvScrub(Base):
    NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_SESSION_ID")

    def test_scrub_removes_names_only_and_keeps_real_environment(self):
        fake = {n: "fake-value-not-real-1234" for n in self.NAMES}
        with mock.patch.dict(os.environ, fake):
            clean, removed = safety.scrub_env()
            for n in self.NAMES:
                self.assertNotIn(n, clean)
                self.assertIn(n, removed)
                self.assertEqual(os.environ[n], "fake-value-not-real-1234")  # user's env untouched
            self.assertIn("PATH", {k.upper() for k in clean})
            self.assertNotIn("fake-value-not-real-1234", json.dumps(removed))

    def test_child_processes_do_not_see_billing_variables(self):
        probe = [PY, "-c", "import os,sys; sys.exit(1 if any(k in os.environ for k in "
                           "('OPENAI_API_KEY','ANTHROPIC_API_KEY','CODEX_API_KEY','ANTHROPIC_BASE_URL')) else 0)"]
        fake = {n: "fake-value-not-real-1234" for n in self.NAMES}
        with mock.patch.dict(os.environ, fake):
            o, _ = self.make(scen([ms.write_step(GOOD)], [ms.review("COMPLETE")]), {
                "verify_command": probe, "command_allowlist": [["python", "-c", probe[2]]], "min_tests_ran": 0})
            st = o.start("t")
            leaked = [e for e in self.events(o) if "fake-value-not-real-1234" in json.dumps(e)]
        self.assertEqual(st["status"], "COMPLETE", "child saw a billing variable or verification failed")
        self.assertEqual(leaked, [])
        start = next(e for e in self.events(o) if e["event"] == "task_start")
        self.assertIn("OPENAI_API_KEY", start["data"]["env_names_removed_for_children"])

    def test_no_forbidden_flags_or_live_cli_calls_in_source(self):
        shim = (ROOT / "aishim.py").read_text(encoding="utf-8")  # names them ONLY in its refusal list
        deny = shim[shim.index("_BYPASS = ("):shim.index(")", shim.index("_BYPASS = ("))]
        self.assertEqual(shim.count("--dangerously"), deny.count("--dangerously"))
        for f in ROOT.glob("*.py"):
            if f.name in ("claude_adapter.py", "sim_claude.py", "aishim.py"):
                continue  # these NAME the forbidden flags in a deny-list; test_claude_adapter checks them via AST + built argv
            code = f.read_text(encoding="utf-8")
            for bad in ("--bare", "--dangerously", "dangerously-bypass"):
                self.assertNotIn(bad, code, f"{f.name} contains {bad}")
            self.assertIsNone(re.search(r"(run_process|Popen|subprocess\.run)\(\s*\[\s*[\"'](codex|claude)", code), f.name)


class TestPathAndCommandPolicy(Base):
    def setUp(self):
        super().setUp()
        self.pol = safety.Policy(self.ws, self.cfg())
        self.fs = safety.GuardedFS(self.pol, os.environ.copy(), 5)

    def test_paths_outside_workspace_or_protected_are_rejected(self):
        outside = str(self.base / "outside.txt")
        for bad in ("../x.txt", "..\\x.txt", "a/../../x.txt", outside, ".env", "sub/.env.local", "key.pem",
                    ".git/config", "id_rsa", "notes.txt:hidden", "", "."):
            with self.subTest(bad), self.assertRaises(safety.PolicyViolation):
                self.fs.write(bad, "x")
        self.assertFalse((self.base / "x.txt").exists() or (self.base / "outside.txt").exists())
        self.fs.write("src/ok.py", "x = 1\n")
        self.assertTrue((self.ws / "src" / "ok.py").exists())

    def test_symlink_escape_is_rejected(self):
        target = self.base / "elsewhere"; target.mkdir()
        try:
            os.symlink(target, self.ws / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted on this system")
        with self.assertRaises(safety.PolicyViolation):
            self.fs.write("link/evil.txt", "x")
        self.assertEqual(list(target.iterdir()), [])

    def test_command_policy(self):
        ok = [["python", "-m", "unittest"], [PY, "-m", "unittest", "discover", "-s", "tests"]]
        bad = [["rm", "-rf", "/"], ["python", "-c", "print(1)"], "python -m unittest", [],
               ["powershell", "-c", "x"], ["C:\\evil\\python.exe", "-m", "unittest"], ["python", "-m", "pip", "install", "x"]]
        for c in ok:
            self.pol.check_command(c)
        for c in bad:
            with self.subTest(c), self.assertRaises(safety.PolicyViolation):
                self.pol.check_command(c)

    def test_orchestrator_blocks_agent_write_outside_workspace(self):
        step = {"ops": [{"op": "write", "path": "../outside.txt", "content": "pwn"}]}
        o, _, st = self.run_task(scen([step], []))
        self.assertEqual(st["status"], "HUMAN_REQUIRED")
        self.assertTrue(st["stop_reason"].startswith("policy_violation"))
        self.assertFalse((self.base / "outside.txt").exists())

    def test_agent_shell_command_is_never_executed(self):
        step = {"ops": [{"op": "run", "argv": ["rm", "-rf", "/"]}]}
        with mock.patch.object(safety, "run_process", wraps=safety.run_process) as spy:
            _, _, st = self.run_task(scen([step], []))
        self.assertEqual(st["status"], "HUMAN_REQUIRED")
        self.assertTrue(all(c.args[0][0] != "rm" for c in spy.call_args_list))

    def test_posthoc_detection_of_protected_write_that_bypassed_the_gateway(self):
        step = {"ops": [{"op": "rogue_write", "path": ".env", "content": "TOKEN=1"}]}
        _, _, st = self.run_task(scen([step], []))
        self.assertEqual(st["status"], "HUMAN_REQUIRED")
        self.assertIn("protected path changed: .env", st["stop_reason"])

    def test_posthoc_detection_of_unapproved_deletion(self):
        step = {"ops": [{"op": "rogue_delete", "path": "tests/test_calc.py"}]}
        _, _, st = self.run_task(scen([step], []))
        self.assertEqual(st["status"], "HUMAN_REQUIRED")
        self.assertIn("unapproved deletion: tests/test_calc.py", st["stop_reason"])

    def test_agent_cannot_expand_permissions_through_its_output(self):
        ex = ms.write_step(GOOD, commands_run=["rm -rf /"])
        ex["report"]["allow_paths"] = ["/"]
        rev = ms.review("COMPLETE", allowed_commands=["rm -rf /"], permissions={"workspace": "/"}, sandbox="off")
        allow_before = json.dumps(self.cfg()["command_allowlist"])
        with mock.patch.object(safety, "run_process", wraps=safety.run_process) as spy:
            o, _, st = self.run_task(scen([ex], [rev]))
        self.assertEqual(st["status"], "COMPLETE")
        self.assertEqual(json.dumps(o.policy.allow), allow_before)
        self.assertEqual(o.policy.approved, set())
        self.assertTrue(all("rm" not in c.args[0] for c in spy.call_args_list))
        self.assertGreaterEqual(self.event_names(o).count("permission_expansion_attempt_ignored"), 2)


class TestEvidence(Base):
    def test_modified_deleted_and_untracked_files_are_all_reported(self):
        (self.ws / "notes.txt").write_text("v1", encoding="utf-8")
        (self.ws / "old.txt").write_text("legacy", encoding="utf-8")
        step = {"ops": [{"op": "write", "path": "calc.py", "content": GOOD},
                        {"op": "write", "path": "notes.txt", "content": "v2"},
                        {"op": "delete", "path": "old.txt"}],
                "report": {"changed_files": ["ghost.py"]}}
        o, (_, _, rv), st = self.run_task(scen([step], [ms.review("COMPLETE")]), {"deletes_require_approval": False})
        ev = rv.calls[0]["evidence"]
        self.assertEqual((ev["diff"]["added"], ev["diff"]["modified"], ev["diff"]["deleted"]),
                         (["calc.py"], ["notes.txt"], ["old.txt"]))
        disc = rv.calls[0]["report_discrepancy"]
        self.assertEqual(disc["claimed_not_changed"], ["ghost.py"])
        self.assertIn("report_discrepancy", self.event_names(o))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_status_evidence_includes_untracked_files(self):
        subprocess.run(["git", "init", "-q"], cwd=self.ws, check=True, capture_output=True)
        _, (_, _, rv), st = self.run_task(scen([ms.write_step(GOOD)], [ms.review("COMPLETE")]))
        self.assertTrue(any(line.endswith("calc.py") and line.startswith("??") for line in rv.calls[0]["evidence"]["git_status"]))

    def test_tests_ran_parser_and_error_classifier(self):
        self.assertEqual(safety.tests_ran("Ran 3 tests in 0.001s\n\nOK"), 3)
        self.assertEqual(safety.tests_ran("== 5 passed in 0.1s =="), 5)
        self.assertIsNone(safety.tests_ran("nothing here"))
        self.assertEqual(safety.classify_cli_error("Error: usage limit reached, try again later"), "quota")
        self.assertEqual(safety.classify_cli_error("HTTP 401 Unauthorized - please log in"), "auth")
        self.assertIsNone(safety.classify_cli_error("syntax error"))


class TestProcessControl(Base):
    def test_timeout_kills_the_whole_process_tree(self):
        pidfile = self.base / "child.pid"
        code = ("import subprocess,sys,time\n"
                "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                "open(sys.argv[1],'w').write(str(c.pid))\n"
                "time.sleep(60)\n")
        t0 = time.time()
        with self.assertRaises(safety.ProcessTimeout):
            safety.run_process([PY, "-c", code, str(pidfile)], cwd=self.base, env=os.environ.copy(), timeout=3)
        self.assertLess(time.time() - t0, 15)
        child = int(pidfile.read_text())
        for _ in range(30):
            if not safety.pid_alive(child):
                break
            time.sleep(0.2)
        self.assertFalse(safety.pid_alive(child), "grandchild process survived the kill")

    def test_cancellation_kills_the_process(self):
        t0 = time.time()
        with self.assertRaises(safety.ProcessCancelled):
            safety.run_process([PY, "-c", "import time; time.sleep(60)"], cwd=self.base, env=os.environ.copy(),
                               timeout=60, cancel_check=lambda: time.time() - t0 > 1)
        self.assertLess(time.time() - t0, 15)

    def test_successful_process_returns_output(self):
        r = safety.run_process([PY, "-c", "print('hi')"], cwd=self.base, env=os.environ.copy(), timeout=20)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "hi"))


class TestLocks(Base):
    def test_second_lock_on_same_workspace_is_refused(self):
        p = store.acquire_lock(self.runs, self.ws, "t1")
        with self.assertRaises(store.WorkspaceLocked):
            store.acquire_lock(self.runs, self.ws, "t2")
        store.release_lock(p, "t1")
        store.release_lock(store.acquire_lock(self.runs, self.ws, "t3"), "t3")

    def test_stale_lock_of_a_dead_process_is_reclaimed(self):
        dead = subprocess.Popen([PY, "-c", "pass"]); dead.wait()
        lock = store._lock_file(self.runs, self.ws)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"task_id": "ghost", "pid": dead.pid}), encoding="utf-8")
        store.release_lock(store.acquire_lock(self.runs, self.ws, "t1"), "t1")

    def test_lock_is_held_while_a_task_runs(self):
        seen = []
        def hook(o, c):
            with self.assertRaises(store.WorkspaceLocked):
                store.acquire_lock(self.runs, self.ws, "intruder")
            seen.append(c)
        steps = [ms.write_step(ms.BAD + f"# {i}\n") for i in range(2)]
        revs = [ms.review("CONTINUE", next_instruction="add() must return a + b")] * 2
        o, _ = self.make(scen(steps, revs), {"max_cycles": 2}, after_cycle=hook)
        o.start("t")
        self.assertEqual(seen, [1, 2])  # hook ran after each cycle, lock held every time
        store.release_lock(store.acquire_lock(self.runs, self.ws, "after"), "after")  # released at the end


if __name__ == "__main__":
    unittest.main()
