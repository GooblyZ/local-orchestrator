"""Browser app: task entry, confirmation, settings, permissions, controls and the security of every new endpoint.
Real Codex/Claude adapters against SIMULATED CLIs; no model is contacted."""
import http.client
import json
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path

from helpers import ms, orch  # noqa: F401
from test_codex_adapter import FakeRunner
from test_mvp import PLAN, MvpBase, cx

import claude_adapter as cla  # noqa: E402
import codex_adapter as ca  # noqa: E402
import web_viewer as wv  # noqa: E402
from app_service import AppService  # noqa: E402
from safety import ProcessCancelled  # noqa: E402
from sim_claude import SimClaude  # noqa: E402

GOOD = ms.GOOD
CATALOG = {"codex": [{"slug": "gpt-6-sol", "name": "GPT-6-Sol", "efforts": ["low", "medium", "high"], "default_effort": "medium"},
                     {"slug": "gpt-6-luna", "name": "GPT-6-Luna", "efforts": ["low", "medium", "high", "xhigh"], "default_effort": "medium"}],
           "claude": {"models": [{"id": "claude-opus-5-5", "name": "Opus 5.5"}, {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"}],
                      "efforts": ["low", "high"]}}
OK_REVIEW = cx(ms.review("COMPLETE", findings=["tests pass"], next_instruction="", question=""))
WRITE_GOOD = {"ops": [{"op": "write", "path": "calc.py", "content": GOOD}, {"op": "write", "path": "tests/test_calc.py", "content": ms.TEST_CALC}]}
AGAIN = cx(ms.review("CONTINUE", findings=["add a docstring"], next_instruction="Add a docstring to add() in calc.py.", question=""))


class Gate(SimClaude):
    """A Claude CLI that blocks inside its call until released or cancelled (cancel_check -> process killed)."""
    def __init__(self, steps):
        super().__init__(steps)
        self.release, self.entered = threading.Event(), threading.Event()

    def __call__(self, argv, **kw):
        if "-p" in argv:
            self.entered.set()
            while not self.release.wait(0.05):
                if kw.get("cancel_check") and kw["cancel_check"]():
                    raise ProcessCancelled("cancelled")
        return super().__call__(argv, **kw)


class AppBase(MvpBase):
    def setUp(self):
        super().setUp()
        self.cfg_path = self.base / "config.json"
        self.cfg_path.write_text(json.dumps(self.full_cfg()), encoding="utf-8")
        self.fr = FakeRunner([], models=json.dumps({"models": [{"slug": "gpt-6-sol"}, {"slug": "gpt-6-luna"}]}))
        self.sim, self.opened = SimClaude([]), []
        self.wsdir = self.base / "workspaces"
        self.svc = AppService(str(self.cfg_path), agent_factory=self.agents, workspaces_dir=self.wsdir,
                              open_folder=self.opened.append, catalog=lambda cfg: CATALOG)
        self.srv = wv.ViewerServer(self.runs, 0, app=self.svc).start()
        self.addCleanup(self.srv.stop)

    def agents(self, cfg, ws):
        codex, claude = ca.CodexCli(cfg, base_dir=self.proj, runner=self.fr), cla.ClaudeCli(cfg, runner=self.sim)
        codex.preflight()
        claude.preflight(str(ws))
        return ca.CodexPlanner(codex), cla.ClaudeExecutor(claude), ca.CodexReviewer(codex)

    def req(self, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        h = {"Origin": f"http://127.0.0.1:{self.srv.port}", "X-Orch-Token": self.srv.token, "X-Orch-CSRF": self.srv.csrf,
             "Content-Type": "application/json"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        try:
            return r.status, json.loads(out)
        except ValueError:
            return r.status, out

    def post(self, path, body, **kw):
        return self.req("POST", path, body, **kw)

    def get(self, path):
        sep = "&" if "?" in path else "?"
        return self.req("GET", f"{path}{sep}t={self.srv.token}")

    def wait_idle(self, timeout=15):
        end = time.time() + timeout
        while self.svc.busy() and time.time() < end:
            time.sleep(0.05)
        self.assertFalse(self.svc.busy(), "the task thread did not finish")

    def start_task(self, task="implement calc.add", settings=None, **extra):
        st, prep = self.post("/api/app/prepare", dict({"task": task, "settings": settings or {}}, **extra))
        self.assertEqual(st, 200, prep)
        st, r = self.post("/api/app/start", {"confirm_id": prep["confirm_id"], "accept_local_execution": True})
        self.assertEqual(st, 200, r)
        return prep, r["task_id"]

    def state(self, tid):
        return json.loads((self.runs / tid / "state.json").read_text(encoding="utf-8"))

    def claude_calls(self):
        return [c for c in self.sim.calls if "-p" in c["argv"]]


class TestEndpointSecurity(AppBase):
    def test_every_state_change_needs_header_token_csrf_origin_and_json(self):
        body = {"task": "x"}
        cases = [({"X-Orch-Token": None}, 403), ({"X-Orch-CSRF": None}, 403), ({"X-Orch-CSRF": "wrong"}, 403),
                 ({"Origin": None}, 403), ({"Origin": "http://evil.example"}, 403), ({"Origin": "null"}, 403),
                 ({"Host": "evil.example"}, 403), ({"Content-Type": "text/plain"}, 415)]
        for hdr, code in cases:
            with self.subTest(hdr=hdr):
                self.assertEqual(self.post("/api/app/prepare", body, headers=hdr)[0], code)
        self.assertEqual(self.post(f"/api/app/prepare?t={self.srv.token}", body)[0], 400, "nothing is accepted from the URL")
        self.assertEqual(self.req("POST", "/api/app/prepare", raw=b"x" * 2_100_000)[0], 413)
        self.assertEqual(self.req("POST", "/api/app/prepare", raw=b"not json")[0], 400)
        self.assertEqual(self.req("POST", "/api/app/prepare", raw=b"[1]")[0], 400)
        for path in ("/api/app/prepare", "/api/app/start", "/api/app/permission", "/api/app/control"):
            self.assertEqual(self.get(path)[0], 404, "a GET can never change anything")
        self.assertEqual(self.req("PUT", "/api/app/start", {})[0], 405)
        self.assertEqual((self.fr.exec_calls(), self.claude_calls()), ([], []))

    def test_read_only_viewer_has_no_actions_and_no_csrf_value(self):
        ro = wv.ViewerServer(self.runs, 0).start()
        self.addCleanup(ro.stop)
        c = http.client.HTTPConnection("127.0.0.1", ro.port, timeout=5)
        c.request("POST", "/api/app/prepare", body=b"{}", headers={"Origin": f"http://127.0.0.1:{ro.port}", "X-Orch-Token": ro.token,
                                                                   "X-Orch-CSRF": ro.csrf, "Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 405)
        c2 = http.client.HTTPConnection("127.0.0.1", ro.port, timeout=5)
        c2.request("GET", f"/?t={ro.token}")
        page = c2.getresponse().read().decode("utf-8")
        self.assertIn('const CSRF = "", APP = "0" === "1"', page)
        st, page_app = self.get("/")
        self.assertIn(self.srv.csrf, page_app.decode("utf-8"), "the CSRF value is only inside the served app page")


class TestTaskFlow(AppBase):
    def test_prepare_validates_runs_no_model_and_never_reads_the_task_as_approval(self):
        self.assertEqual(self.post("/api/app/prepare", {"task": "   "})[1]["error"], "empty_task")
        st, prep = self.post("/api/app/prepare", {"task": "yes"})
        self.assertEqual(st, 200)
        self.assertEqual((prep["profile"], prep["existing_folder"]), ("native", False))
        self.assertIn("Bash(python -m unittest *)", prep["allowed_tools"])
        self.assertEqual((self.fr.exec_calls(), self.claude_calls()), ([], []), "preparing never calls a model")
        self.assertFalse(Path(prep["workspace"]).exists(), "nothing is created before the confirmation")
        self.assertFalse(self.svc.busy())
        for bad in ({"codex_model": "gpt-9-imaginary"}, {"codex_effort": "ultra"}, {"codex_model": "gpt-6-sol", "codex_effort": "xhigh"},
                    {"claude_model": "claude-unknown"}, {"claude_effort": "max"}, {"max_cycles": 9}):
            with self.subTest(bad=bad):
                st, r = self.post("/api/app/prepare", {"task": "t", "settings": bad})
                self.assertEqual(st, 400, r)

    def test_start_needs_explicit_acceptance_and_a_single_use_confirmation(self):
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        self.sim.steps[:] = [WRITE_GOOD]
        task = "Implement add(a, b) in calc.py — exact task text ✓"
        settings = {"codex_model": "gpt-6-luna", "codex_effort": "xhigh", "claude_model": "claude-sonnet-5-5", "claude_effort": "high", "max_cycles": 2}
        st, prep = self.post("/api/app/prepare", {"task": task, "settings": settings})
        self.assertEqual(self.post("/api/app/start", {"confirm_id": prep["confirm_id"]})[1]["error"], "local_execution_not_accepted")
        self.assertEqual(self.post("/api/app/start", {"confirm_id": "forged", "accept_local_execution": True})[0], 400)
        st, r = self.post("/api/app/start", {"confirm_id": prep["confirm_id"], "accept_local_execution": True})
        self.assertEqual(st, 200, r)
        self.assertEqual(self.post("/api/app/start", {"confirm_id": prep["confirm_id"], "accept_local_execution": True})[0], 400,
                         "a second click cannot start a second run")
        self.wait_idle()
        s = self.state(r["task_id"])
        self.assertEqual(s["status"], "COMPLETE", s["stop_reason"])
        self.assertEqual(s["task"], task)
        self.assertIn(task, self.fr.exec_calls()[0]["kw"]["stdin_text"], "Codex receives the exact task text")
        self.assertTrue(Path(s["workspace"]).is_relative_to(self.wsdir))
        self.assertEqual(s["agent_settings"], {"codex_model": "gpt-6-luna", "codex_effort": "xhigh", "claude_model": "claude-sonnet-5-5", "claude_effort": "high"})
        a_cx, a_cl = self.fr.exec_calls()[0]["argv"], self.claude_calls()[0]["argv"]
        self.assertEqual(a_cx[a_cx.index("-m") + 1], "gpt-6-luna"); self.assertIn('model_reasoning_effort="xhigh"', a_cx)
        self.assertEqual((a_cl[a_cl.index("--model") + 1], a_cl[a_cl.index("--effort") + 1]), ("claude-sonnet-5-5", "high"))
        self.assertEqual(self.get("/api/app/options")[1]["defaults"]["codex_model"], "gpt-6-luna", "preferences persist")
        snap = self.get(f"/api/snapshot?task={r['task_id']}")[1]
        self.assertEqual(snap["status"]["agent_settings"]["claude_model"], "claude-sonnet-5-5", "the viewer shows the active models")

    def test_existing_folder_only_with_explicit_authorization(self):
        folder = self.new_ws("existing")
        self.assertEqual(self.post("/api/app/prepare", {"task": "t", "folder": str(folder)})[1]["error"], "folder_not_authorized")
        self.assertEqual(self.post("/api/app/prepare", {"task": "t", "folder": str(Path.home()), "folder_authorized": True})[0], 400)
        st, prep = self.post("/api/app/prepare", {"task": "t", "folder": str(folder), "folder_authorized": True})
        self.assertEqual((st, prep["workspace"], prep["existing_folder"]), (200, str(folder.resolve()), True))

    def test_duplicate_launch_is_refused_while_a_task_runs_and_cancel_stops_it(self):
        self.sim = Gate([WRITE_GOOD])
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        _, tid = self.start_task()
        self.assertTrue(self.sim.entered.wait(10))
        st, prep2 = self.post("/api/app/prepare", {"task": "second"})
        self.assertEqual(self.post("/api/app/start", {"confirm_id": prep2["confirm_id"], "accept_local_execution": True})[0], 409)
        self.assertEqual(self.get(f"/api/snapshot?task={tid}")[1]["status"]["run_state"], "active")
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "stop"})[0], 200)
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "STOPPED")

    def test_pause_then_resume_finishes_the_task(self):
        self.sim = Gate([WRITE_GOOD, WRITE_GOOD])
        self.fr.execs[:] = [PLAN, AGAIN, OK_REVIEW]  # a pause takes effect between cycles
        _, tid = self.start_task()
        self.assertTrue(self.sim.entered.wait(10))
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "pause"})[0], 200)
        self.sim.release.set()
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "PAUSED")
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 200)
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "COMPLETE")

    def test_permission_approval_from_the_browser_is_exact_and_resumes_the_session(self):
        denial = {"envelope": {"permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "npm test"}}]},
                  "report": {"summary": "need npm", "changed_files": [], "commands_run": [], "remaining_issues": []}}
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        self.sim.steps[:] = [denial, WRITE_GOOD]
        _, tid = self.start_task()
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual(s["status"], "HUMAN_REQUIRED")
        snap = self.get(f"/api/snapshot?task={tid}")[1]["status"]
        self.assertEqual(snap["pending_permissions"][0]["suggested_rule"], "Bash(npm test)")
        for bad in ({"decision": "approve", "rules": ["Bash"]}, {"decision": "approve", "rules": ["Bash(npm test)", "Bash(npm *)"]},
                    {"decision": "approve", "rules": []}, {"decision": "maybe"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.post("/api/app/permission", dict(bad, task_id=tid))[0], 400)
        self.assertEqual(self.state(tid)["approved_tools"], [], "a refused request grants nothing")
        st, r = self.post("/api/app/permission", {"task_id": tid, "decision": "approve", "rules": ["Bash(npm test)"]})
        self.assertEqual(st, 200, r)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual(s["status"], "COMPLETE", s["stop_reason"])
        second = self.claude_calls()[1]["argv"]
        self.assertIn("Bash(npm test)", second[second.index("--allowedTools") + 1])
        self.assertEqual(second[second.index("--resume") + 1], self.claude_calls()[0]["argv"][self.claude_calls()[0]["argv"].index("--session-id") + 1])

    def test_permission_denial_from_the_browser(self):
        denial = {"envelope": {"permission_denials": [{"tool_name": "WebFetch", "tool_input": {"url": "https://x"}}]},
                  "report": {"summary": "s", "changed_files": [], "commands_run": [], "remaining_issues": []}}
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        self.sim.steps[:] = [denial, WRITE_GOOD]
        _, tid = self.start_task()
        self.wait_idle()
        self.assertEqual(self.post("/api/app/permission", {"task_id": tid, "decision": "deny"})[0], 200)
        self.wait_idle()
        self.assertEqual(self.state(tid)["approved_tools"], [])
        self.assertIn("did NOT approve: WebFetch", self.sim.prompts[1])

    def test_files_listing_reading_and_opening(self):
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        self.sim.steps[:] = [WRITE_GOOD]
        _, tid = self.start_task()
        self.wait_idle()
        st, f = self.get(f"/api/app/files?task={tid}")
        self.assertEqual(sorted(x["path"] for x in f["files"]), ["calc.py", "tests/test_calc.py"])
        self.assertEqual(self.get(f"/api/app/file?task={tid}&path=calc.py")[1]["content"], GOOD)
        self.assertEqual(self.get(f"/api/app/file?task={tid}&path=..%2F..%2Fconfig.json")[0], 403)
        self.assertEqual(self.get(f"/api/app/file?task=..%2Fx&path=calc.py")[0], 400)
        self.assertEqual(self.post("/api/app/open-folder", {"task_id": tid})[0], 200)
        self.assertEqual(self.opened, [self.state(tid)["workspace"]])

    def test_errors_are_reported_and_no_model_is_called(self):
        self.write_config("not valid toml [")  # Codex preflight now fails
        st, r = self.post("/api/app/prepare", {"task": "t"})
        self.assertEqual(st, 503)
        self.assertTrue(r["error"].startswith("BLOCKED"))
        self.assertEqual(self.fr.exec_calls(), [])


class TestGoalModeInTheApp(AppBase):
    def test_goal_settings_are_validated_and_a_budget_pause_is_extended_from_the_browser(self):
        for bad in ({"mode": "forever"}, {"kind": "trading"}, {"max_minutes": 0}, {"checkpoint_cycles": 99999}, {"max_codex_calls": "10"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.post("/api/app/prepare", {"task": "t", "settings": bad})[0], 400)
        AGAIN2 = cx(ms.review("CONTINUE", findings=["x"], next_instruction="Second pass: add a docstring to add().", question=""))
        self.fr.execs[:] = [PLAN, AGAIN2, OK_REVIEW]
        self.sim.steps[:] = [WRITE_GOOD, WRITE_GOOD]
        prep, tid = self.start_task(settings={"mode": "goal", "kind": "code", "checkpoint_cycles": 10, "max_minutes": 30,
                                              "max_codex_calls": 9, "max_claude_calls": 1})
        self.assertEqual((prep["settings"]["mode"], prep["max_codex_calls"]), ("goal", 9))
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual((s["status"], s["stop_reason"]), ("PAUSED", "budget_reached: claude calls 1/1"))
        snap = self.get(f"/api/snapshot?task={tid}")[1]["status"]
        self.assertEqual((snap["mode"], snap["budget"]["max_claude_calls"], snap["calls"]), ("goal", 1, {"codex": 2, "claude": 1}))
        self.assertEqual(self.post("/api/app/budget", {"task_id": tid, "add": {"max_claude_calls": -1}})[0], 400)
        self.assertEqual(self.post("/api/app/budget", {"task_id": tid, "add": {"max_claude_calls": 2}}, headers={"X-Orch-CSRF": "x"})[0], 403)
        self.assertEqual(self.post("/api/app/budget", {"task_id": tid, "add": {"max_claude_calls": 2}})[0], 200)
        self.wait_idle()
        s = self.state(tid)
        self.assertEqual((s["status"], s["cycle"], s["budget"]["max_claude_calls"]), ("COMPLETE", 2, 3), s["stop_reason"])
        src = (Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for needed in ("עבודה עד להשגת המטרה", "עבודה עם מגבלת סבבים", "עוד שעה (+60 דקות), והמשך", "מטרה והתקדמות", "INCONCLUSIVE",
                       "מחקר (נבדק לפי דוח וראיות)", "הסוכנים לא מתקדמים"):
            self.assertIn(needed, src)


class TestMockRunsStayMock(AppBase):
    def test_app_actions_on_a_mock_run_never_reach_real_agents(self):
        from agents import make_mock_agents
        from goal_mode import make_goal
        planner, executor, reviewer = make_mock_agents({"planner": [ms.plan()], "executor": [ms.write_step(GOOD)],
                                                        "reviewer": [ms.review("CONTINUE", next_instruction="again: return a + b")]})
        o = orch.Orchestrator(self.cfg(), self.ws, self.runs, planner, executor, reviewer, out=lambda m: None,
                              goal=make_goal("goal", "code", {"max_claude_calls": 1}))
        self.assertEqual(o.start("t")["status"], "PAUSED")
        for path, body in (("/api/app/budget", {"task_id": o.task_id, "add": {"max_claude_calls": 1}}),
                           ("/api/app/control", {"task_id": o.task_id, "action": "resume"})):
            with self.subTest(path=path):
                st, r = self.post(path, body)
                self.assertEqual(st, 409, r)
                self.assertIn("MOCK", r["error"])
        self.assertEqual((self.fr.exec_calls(), self.claude_calls()), ([], []))
        self.assertEqual(self.state(o.task_id)["budget"]["max_claude_calls"], 1, "nothing was extended")


class TestAppLauncher(AppBase):
    def test_a_second_launch_reuses_the_running_app(self):
        import launcher
        a = Namespace(config=str(self.cfg_path), port=0, no_open=True)
        out1, out2 = [], []
        srv = launcher.run_app(a, out=out1.append, service=self.svc, serve_forever=False)
        self.addCleanup(srv.stop)
        self.assertTrue((self.runs / "_app.json").exists())
        self.assertEqual(launcher.run_app(a, out=out2.append, service=self.svc, serve_forever=False), 0)
        self.assertIn("כבר פועלת", out2[0])
        page = launcher.running_app(self.runs / "_app.json")
        self.assertTrue(page.startswith(f"http://127.0.0.1:{srv.port}/?t="))
        src = (Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for needed in ("Codex + Claude", "מה תרצה שהם יעשו?", "התחל משימה", "הגדרות סוכנים", "אשר פעולה זו", "דחה",
                       "אני מאשר הרצה מקומית", "השהה", "בטל משימה", "המודלים בשימוש", "X-Orch-CSRF"):
            self.assertIn(needed, src)
        cmd = (Path(orch.__file__).parent / "start.cmd").read_text(encoding="ascii")
        self.assertIn("orchestrator.py app", cmd)


if __name__ == "__main__":
    unittest.main()
