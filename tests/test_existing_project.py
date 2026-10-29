"""Existing-project mode: the owner authorizes a real project folder; both agents are directed at it; nothing falls back to
a workspace copy; a saved task's context is handed off instead of rebinding the saved task. Real Codex/Claude adapters
against SIMULATED CLIs; no model is contacted."""
import json
import os
import shutil
import unittest
from pathlib import Path

from helpers import ms, orch  # noqa: F401
from test_app import AGAIN, OK_REVIEW, WRITE_GOOD, AppBase
from test_mvp import PLAN, cx

import handoff  # noqa: E402
from config import HERE, OrchestratorError, validate_project_root  # noqa: E402
from safety import snapshot  # noqa: E402

QUESTION = cx(ms.review("HUMAN_REQUIRED", findings=["the owner must choose the export format"], next_instruction="",
                        question="Which export format should the studio use?"))


class ProjectBase(AppBase):
    def setUp(self):
        super().setUp()
        self.project = ms.make_workspace(self.base / "real-project")
        (self.project / "README_HE.md").write_text("# real project\n", encoding="utf-8")
        (self.project / "data").mkdir()
        (self.project / "data" / "app.db").write_bytes(b"live data")

    def prepare_existing(self, task="implement calc.add", **extra):
        body = {"task": task, "settings": {}, "project_mode": "existing_project", "folder": str(self.project),
                "folder_authorized": True}
        body.update(extra)
        return self.post("/api/app/prepare", body)

    def start_existing(self, task="implement calc.add", **extra):
        st, prep = self.prepare_existing(task, **extra)
        self.assertEqual(st, 200, prep)
        st, r = self.post("/api/app/start", {"confirm_id": prep["confirm_id"], "accept_local_execution": True})
        self.assertEqual(st, 200, r)
        return prep, r["task_id"]

    def codex_prompts(self):
        return [c["kw"]["stdin_text"] for c in self.fr.exec_calls()]


class TestPathValidation(ProjectBase):
    def check(self, path, fragment, **kw):
        with self.assertRaises(OrchestratorError) as e:
            validate_project_root(path, self.runs, self.full_cfg(), **kw)
        self.assertIn(fragment, str(e.exception))

    def test_only_an_absolute_real_project_folder_is_accepted(self):
        cfg = self.full_cfg()
        self.assertEqual(validate_project_root(str(self.project), self.runs, cfg), Path(os.path.realpath(self.project)))
        self.assertEqual(validate_project_root(f'"{self.project}"', self.runs, cfg), Path(os.path.realpath(self.project)))
        self.check("", "no folder given")
        self.check("real-project", "absolute")
        self.check(str(self.project / "missing"), "not a directory")
        self.check(str(self.project / "README_HE.md"), "not a directory")
        self.check(str(Path.home()), "home")
        self.check(str(Path.home().parent), "")  # refused (it contains the home folder and the orchestrator)
        self.check(str(self.runs.parent), "orchestrator/runs")  # contains the runs directory
        (self.runs / "x").mkdir(parents=True)
        self.check(str(self.runs / "x"), "orchestrator/runs")
        self.check(str(HERE), "orchestrator")
        old_copy = HERE / "workspaces"  # where the orchestrator keeps its isolated copies (read-only check)
        if old_copy.is_dir():
            self.check(str(old_copy), "inside the orchestrator")
        (self.project / ".git" / "sub").mkdir(parents=True)
        self.check(str(self.project / ".git" / "sub"), "protected location (.git)")
        self.check(str(self.project), "too large", max_files=2)

    def test_validation_reads_but_never_writes(self):
        before = snapshot(self.project, [])
        validate_project_root(str(self.project), self.runs, self.full_cfg())
        self.assertEqual(snapshot(self.project, []), before)


class TestNoFallback(ProjectBase):
    def test_existing_mode_without_a_usable_folder_is_refused_never_replaced(self):
        for body, err in (({"folder": ""}, "project_folder_required"), ({"folder": "   "}, "project_folder_required"),
                          ({}, "project_folder_required"), ({"folder": str(self.project)}, "folder_not_authorized"),
                          ({"folder": "relative\\dir", "folder_authorized": True}, "folder_invalid"),
                          ({"folder": str(self.project / "nope"), "folder_authorized": True}, "folder_invalid")):
            with self.subTest(body=body):
                st, r = self.post("/api/app/prepare", dict({"task": "t", "project_mode": "existing_project"}, **body))
                self.assertEqual(st, 400, r)
                self.assertTrue(r["error"].startswith(err), r)
        for body in ({"project_mode": "new_workspace", "folder": str(self.project)}, {"project_mode": "new_workspace", "handoff_from": "x"},
                     {"project_mode": "somewhere"}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/app/prepare", dict({"task": "t"}, **body))[0], 400)
        self.assertFalse(self.wsdir.exists() and any(self.wsdir.iterdir()), "no workspace was created instead")
        self.assertEqual(self.svc.prepared, {})
        self.assertEqual((self.fr.exec_calls(), self.claude_calls()), ([], []))

    def test_folder_is_checked_again_when_the_owner_confirms(self):
        st, prep = self.prepare_existing()
        self.assertEqual((st, prep["project_mode"], prep["project_root"]), (200, "existing_project", str(self.project.resolve())))
        moved = self.base / "moved-away"
        self.project.rename(moved)
        st, r = self.post("/api/app/start", {"confirm_id": prep["confirm_id"], "accept_local_execution": True})
        self.assertEqual(st, 409, r)
        self.assertTrue(r["error"].startswith("folder_invalid"), r)
        self.assertFalse(self.svc.busy())
        self.assertEqual((self.fr.exec_calls(), self.claude_calls()), ([], []))
        self.assertFalse(self.project.exists(), "nothing is recreated at the old path")


class TestBothAgentsInTheProject(ProjectBase):
    def test_claude_works_in_the_root_and_codex_gets_the_root_content_without_access(self):
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        self.sim.steps[:] = [WRITE_GOOD]
        prep, tid = self.start_existing()
        self.wait_idle()
        s, root = self.state(tid), prep["project_root"]
        self.assertEqual(s["status"], "COMPLETE", s["stop_reason"])
        self.assertEqual((s["workspace"], s["project"]["mode"], s["project"]["root"]), (root, "existing_project", root))
        self.assertTrue((self.project / "calc.py").is_file(), "the change landed in the real project")
        self.assertEqual((self.project / "data" / "app.db").read_bytes(), b"live data")
        self.assertEqual(list(self.wsdir.glob("*")) if self.wsdir.exists() else [], [], "no workspace copy was made")
        for c in self.claude_calls():  # Claude Code's own confinement: working directory + --restricted, never --add-dir
            self.assertEqual(c["kw"]["cwd"], root)
            self.assertIn("--restricted", c["argv"])
            self.assertNotIn("--add-dir", c["argv"])
        self.assertIn(f"AUTHORIZED PROJECT ROOT: {root}", self.sim.prompts[0])
        for c in self.fr.exec_calls():  # Codex: an empty temporary folder, read-only, never the project
            run_dir = Path(c["argv"][c["argv"].index("-C") + 1])
            self.assertEqual(c["argv"][c["argv"].index("-s") + 1], "read-only")
            self.assertFalse(run_dir.is_relative_to(root))
            self.assertEqual(c["kw"]["cwd"], str(run_dir))
        plan_prompt, review_prompt = self.codex_prompts()
        self.assertIn("EXISTING PROJECT FILES", plan_prompt)
        self.assertIn('"path": "README_HE.md"', plan_prompt)
        self.assertIn("WORKING FOLDER", review_prompt)
        self.assertIn("def add(a, b)", review_prompt, "the reviewer judges the real changed file read from the project")
        for p in (plan_prompt, review_prompt):
            self.assertNotIn(root, p, "Codex never receives local paths")
        snap = self.get(f"/api/snapshot?task={tid}")[1]["status"]
        self.assertEqual((snap["project"]["mode"], snap["project"]["root"]), ("existing_project", root))
        self.assertEqual(self.get(f"/api/app/file?task={tid}&path=..%2Freal-project-sibling.txt")[0], 403)

    def test_a_long_task_file_never_overwrites_a_project_file(self):
        (self.project / ".orchestrator").mkdir()
        (self.project / ".orchestrator" / "TASK.md").write_text("the project's own file", encoding="utf-8")
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        self.sim.steps[:] = [WRITE_GOOD]
        _, tid = self.start_existing("implement calc.add " + "x" * 5000)
        self.wait_idle()
        self.assertEqual((self.project / ".orchestrator" / "TASK.md").read_text(encoding="utf-8"), "the project's own file")
        self.assertEqual(self.state(tid)["task_file"]["path"], f".orchestrator/TASK-{tid}.md")
        self.assertTrue((self.project / ".orchestrator" / f"TASK-{tid}.md").is_file())

    def test_deletions_and_protected_changes_in_the_project_stop_the_task(self):
        for name, op in (("deletion", {"op": "delete", "path": "data/app.db"}),
                         ("protected", {"op": "write", "path": ".env", "content": "SECRET=x"})):
            with self.subTest(name=name):
                if not (self.project / "data" / "app.db").exists():
                    (self.project / "data" / "app.db").write_bytes(b"live data")
                self.fr.execs[:] = [PLAN, OK_REVIEW]
                self.sim.steps[:] = [{"ops": [op]}]
                _, tid = self.start_existing()
                self.wait_idle()
                s = self.state(tid)
                self.assertEqual(s["status"], "HUMAN_REQUIRED")
                self.assertIn("posthoc_policy_violation", s["stop_reason"])
                self.assertIn("unapproved deletion: data/app.db" if name == "deletion" else "protected path changed: .env",
                              s["stop_reason"])
                self.assertEqual(len(self.fr.exec_calls()), 1, "no review and no further cycle after the violation")
                self.fr.calls.clear()
                self.post("/api/app/control", {"task_id": tid, "action": "stop"})

    def test_permission_approval_resumes_in_the_same_root_and_a_missing_root_blocks_it(self):
        denial = {"envelope": {"permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "npm test"}}]},
                  "report": {"summary": "need npm", "changed_files": [], "commands_run": [], "remaining_issues": []}}
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        self.sim.steps[:] = [denial, WRITE_GOOD]
        prep, tid = self.start_existing()
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "HUMAN_REQUIRED")
        away = self.base / "away"
        self.project.rename(away)
        st, r = self.post("/api/app/permission", {"task_id": tid, "decision": "approve", "rules": ["Bash(npm test)"]})
        self.assertEqual(st, 409, r)
        self.assertTrue(r["error"].startswith("project_root_invalid"), r)
        self.assertEqual((len(self.claude_calls()), self.state(tid)["approved_tools"]), (1, []), "nothing ran, nothing was granted")
        away.rename(self.project)
        st, r = self.post("/api/app/permission", {"task_id": tid, "decision": "approve", "rules": ["Bash(npm test)"]})
        self.assertEqual(st, 200, r)
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "COMPLETE")
        first, second = (c["argv"] for c in self.claude_calls())
        self.assertEqual(self.claude_calls()[1]["kw"]["cwd"], prep["project_root"])
        self.assertEqual(second[second.index("--resume") + 1], first[first.index("--session-id") + 1])

    def test_pause_and_resume_stay_in_the_root(self):
        from test_app import Gate
        self.sim = Gate([WRITE_GOOD, WRITE_GOOD])
        self.fr.execs[:] = [PLAN, AGAIN, OK_REVIEW]
        prep, tid = self.start_existing()
        self.assertTrue(self.sim.entered.wait(10))
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "pause"})[0], 200)
        self.sim.release.set()
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "PAUSED")
        self.assertEqual(self.post("/api/app/control", {"task_id": tid, "action": "resume"})[0], 200)
        self.wait_idle()
        self.assertEqual(self.state(tid)["status"], "COMPLETE")
        self.assertEqual({c["kw"]["cwd"] for c in self.claude_calls()}, {prep["project_root"]})

    def test_the_engine_refuses_a_workspace_that_is_not_the_authorized_root(self):
        with self.assertRaises(OrchestratorError):
            orch.Orchestrator(self.full_cfg(), self.ws, self.runs, *self.agents(self.full_cfg(), self.ws), out=lambda m: None,
                              project={"mode": handoff.EXISTING, "root": str(self.project)})


class TestHandoff(ProjectBase):
    def saved_source(self):
        """A real (simulated-CLI) task in an isolated workspace that stopped with Codex's question to the owner."""
        self.fr.execs[:] = [PLAN, QUESTION]
        self.sim.steps[:] = [WRITE_GOOD]
        # supervised: Codex's (technical) question stops for the owner, as in the saved Milo task
        _, src = self.start_task("ORIGINAL MILO-LIKE TASK: build the studio", settings={"autonomy": "supervised"})
        self.wait_idle()
        self.assertEqual(self.state(src)["status"], "HUMAN_REQUIRED")
        self.fr.calls.clear()
        self.sim.calls.clear()
        return src

    def test_handoff_reuses_the_saved_context_without_rebinding_or_copying(self):
        src = self.saved_source()
        src_dir, src_ws = self.runs / src, Path(self.state(src)["workspace"])
        saved = {n: (src_dir / n).read_bytes() for n in ("state.json", "transcript.jsonl", "events.jsonl", "baseline.json")}
        ws_before, proj_before = snapshot(src_ws, []), snapshot(self.project, [])
        st, prep = self.prepare_existing("Continue in the deployed project; use MP4.", handoff_from=src)
        self.assertEqual(st, 200, prep)
        self.assertEqual({n: (src_dir / n).read_bytes() for n in saved}, saved, "preparing never touches the saved task")
        task = prep["task"]
        self.assertTrue(task.startswith("Continue in the deployed project; use MP4."))
        for needed in (f"CONTEXT HANDED OFF FROM SAVED TASK {src}", "ORIGINAL MILO-LIKE TASK: build the studio",
                       "Which export format should the studio use?", "acceptance_criteria", "NOT authoritative"):
            self.assertIn(needed, task)
        self.assertNotIn(str(src_ws), task, "the old folder is never offered to the agents as a place to work")
        self.assertEqual((prep["handoff"]["from_task"], prep["handoff"]["from_workspace"]), (src, str(src_ws)))
        self.fr.execs[:] = [PLAN, OK_REVIEW]
        self.sim.steps[:] = [WRITE_GOOD]
        st, r = self.post("/api/app/start", {"confirm_id": prep["confirm_id"], "accept_local_execution": True})
        self.assertEqual(st, 200, r)
        new = r["task_id"]
        self.wait_idle()
        s = self.state(new)
        self.assertEqual(s["status"], "COMPLETE", s["stop_reason"])
        self.assertEqual((s["workspace"], s["project"]["handoff"]["from_task"]), (prep["project_root"], src))
        self.assertEqual({c["kw"]["cwd"] for c in self.claude_calls()}, {prep["project_root"]})
        self.assertIn("--session-id", self.claude_calls()[0]["argv"], "a NEW Claude session, never the old copy's session")
        self.assertEqual(snapshot(src_ws, []), ws_before, "the old workspace copy is never written")
        after = snapshot(self.project, [".orchestrator"])
        mine = {"calc.py", "tests/test_calc.py"}  # exactly what the (simulated) agent wrote
        self.assertEqual({k: v for k, v in after.items() if k not in mine}, {k: v for k, v in proj_before.items() if k not in mine},
                         "only the agent's own change; nothing synchronized from the copy")
        old = self.state(src)  # the source keeps its status and history and only gains the marker
        self.assertEqual((old["status"], old["stop_reason"], old["cycle"]), ("HUMAN_REQUIRED", "reviewer_requested_human", 1))
        self.assertEqual([h["task_id"] for h in old["handed_off_to"]], [new])
        self.assertEqual((src_dir / "transcript.jsonl").read_bytes()[:len(saved["transcript.jsonl"])], saved["transcript.jsonl"])
        for path, body in (("/api/app/control", {"task_id": src, "action": "resume"}),
                           ("/api/app/continue", {"task_id": src, "confirm": True, "note": "go"}),
                           ("/api/app/budget", {"task_id": src, "add": {"max_minutes": 10}})):
            with self.subTest(path=path):
                st, r = self.post(path, body)
                self.assertEqual(st, 409, r)
                self.assertTrue(r["error"].startswith("handed_off"), r)
        self.assertEqual(self.claude_calls()[1:], [], "nothing ran in the old copy")
        with self.assertRaises(OrchestratorError):  # the command-line resume path refuses it too
            orch.Orchestrator(self.full_cfg(), src_ws, self.runs, *self.agents(self.full_cfg(), src_ws), task_id=src,
                              out=lambda m: None, local_execution=True).resume()
        listed = {t["id"]: t for t in self.get("/api/tasks")[1]["tasks"]}
        self.assertEqual((listed[src]["needs_owner"], listed[src]["handed_off"]), (False, True))
        self.assertEqual(listed[new]["project_mode"], "existing_project")

    def test_handoff_sources_are_validated(self):
        src = self.saved_source()
        for bad in ("../x", "nope-000000-000000", "20990101-000000-abcdef"):
            with self.subTest(bad=bad):
                st, r = self.prepare_existing(handoff_from=bad)
                self.assertEqual(st, 400, r)
        st, r = self.post("/api/app/prepare", {"task": "t", "project_mode": "new_workspace", "handoff_from": src})
        self.assertEqual(st, 400, "a handoff always targets an authorized existing project")


class TestUi(unittest.TestCase):
    def test_the_page_offers_the_mode_and_shows_the_root(self):
        src = (Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for needed in ("עבודה על פרויקט קיים", "תיקייה חדשה ומבודדת", "פרויקט קיים מאושר", "project_mode", "handoff_from",
                       "function rootBar", "המשימה הועברה", "project_folder_required", "!(st.handed_off_to || []).length"):
            self.assertIn(needed, src)


if __name__ == "__main__":
    unittest.main()
