"""Fixtures and scripted scenarios for MOCK agents (used by tests/ and the `demo` command)."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

TEST_CALC = '''import unittest
from calc import add


class TestCalc(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_add_negative(self):
        self.assertEqual(add(-1, 1), 0)


if __name__ == "__main__":
    unittest.main()
'''
GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"


def make_workspace(path) -> Path:
    """A tiny project whose existing tests describe the task: implement calc.add."""
    p = Path(path)
    (p / "tests").mkdir(parents=True, exist_ok=True)
    (p / "tests" / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    return p


def plan(instruction="Implement add(a, b) in calc.py so the tests in tests/ pass."):
    return {"plan": "Create calc.py with add(); run the project tests.",
            "acceptance_criteria": ["tests/test_calc.py passes", "no unrelated files modified"],
            "instruction": instruction}


def write_step(content, path="calc.py", **report):
    return {"ops": [{"op": "write", "path": path, "content": content}], "report": report}


def review(verdict="COMPLETE", **kw):
    d = {"verdict": verdict, "findings": kw.pop("findings", ["reviewed"])}
    d.update(kw)
    return d


def success_scenario():
    return {"planner": [plan()], "executor": [write_step(GOOD)],
            "reviewer": [review("COMPLETE", findings=["calc.add implemented; tests pass"])]}


def rejection_scenario():
    """Cycle 1 ships a bug; Codex-mock rejects with actionable feedback; cycle 2 fixes it."""
    return {"planner": [plan()],
            "executor": [write_step(BAD), write_step(GOOD)],
            "reviewer": [review("CONTINUE", findings=["add() subtracts"],
                                next_instruction="add() returns a - b; it must return a + b."),
                         review("COMPLETE")]}


HE_SUMMARY_TASK = "דוגמת MOCK לתקצירים בעברית: לממש את calc.add ולוודא שכל הבדיקות עוברות"


def he_summary_scenario():
    """MOCK ONLY: shows what agent-written `user_summary_he` will look like. These Hebrew texts were written in advance
    for the demo; no model wrote them. Same two-cycle flow as rejection_scenario()."""
    p = plan()
    p["user_summary_he"] = ("אבקש מ-Claude לכתוב את הפונקציה add בקובץ calc.py. המשימה תיחשב גמורה רק אם הבדיקות "
                            "הקיימות בפרויקט יעברו ושום קובץ אחר לא ישתנה.")
    return {"planner": [p],
            "executor": [write_step(BAD, changed_files=["calc.py"], summary="Created calc.py with add().",
                                    user_summary_he="יצרתי את הקובץ calc.py עם הפונקציה add. לא הרצתי בדיקות בעצמי."),
                         write_step(GOOD, changed_files=["calc.py"], summary="Fixed add() to return a + b.",
                                    user_summary_he="תיקנתי את הפונקציה כך שתחזיר a + b במקום a - b. לא שיניתי קבצים אחרים.")],
            "reviewer": [review("CONTINUE", findings=["add() subtracts"], next_instruction="add() returns a - b; it must return a + b.",
                                question="", user_summary_he="הבדיקות האוטומטיות הורצו ונכשלו: הפונקציה מחסרת את המספרים "
                                                             "במקום לחבר אותם. ביקשתי מ-Claude לתקן את השורה הזו."),
                         review("COMPLETE", findings=["calc.add implemented; tests pass"], next_instruction="", question="",
                                user_summary_he="המערכת הריצה את הבדיקות והן עברו. הקוד נכתב לפרויקט ועונה על הדרישות, "
                                                "ולכן אישרתי את העבודה.")]}


PROPOSAL_TASK = "דוגמת MOCK ליצירת קבצים מבוקרת: לכתוב את calc.add בקובץ calc.py (Claude מציע, המערכת כותבת ובודקת בלי להריץ)"
SYNTAX_BAD = "def add(a, b)\n    return a + b\n"


def proposal_step(files, **report):
    """A no-tools executor reply: complete proposed files plus report fields. It performs NO file operations."""
    return {"ops": [], "report": dict({"files": files, "changed_files": [f["path"] for f in files if isinstance(f, dict)]}, **report)}


def proposal_scenario():
    """MOCK ONLY (Phase 4d): cycle 1 proposes forbidden paths (rejected, nothing written); cycle 2 an allowed file with a
    syntax error (written, static check fails); cycle 3 a correct file (written, static check passes, approved)."""
    p = plan("Propose calc.py containing add(a, b) that returns a + b. Propose complete file contents only.")
    p["user_summary_he"] = "אבקש מ-Claude להציע קובץ calc.py עם הפונקציה add. המערכת תכתוב רק את הקובץ שאישרת ותבדוק אותו בלי להריץ."
    return {"planner": [p],
            "executor": [proposal_step([{"path": "../outside.py", "content": "x = 1\n"}, {"path": ".env", "content": "A=1\n"},
                                        {"path": "calc.py", "content": GOOD}], summary="Proposed calc.py plus two helper files.",
                                       user_summary_he="הצעתי את calc.py ועוד שני קבצי עזר."),
                         proposal_step([{"path": "calc.py", "content": SYNTAX_BAD}], summary="Proposed only calc.py.",
                                       user_summary_he="הצעתי רק את calc.py."),
                         proposal_step([{"path": "calc.py", "content": GOOD}], summary="Fixed the missing colon.",
                                       user_summary_he="תיקנתי את הנקודתיים החסרות בשורת ההגדרה.")],
            "reviewer": [review("CONTINUE", findings=["The proposal was rejected: two paths are outside the allowlist."],
                                next_instruction="Propose ONLY calc.py; no other paths.", question="",
                                user_summary_he="המערכת דחתה את ההצעה כי היו בה קבצים לא מורשים. ביקשתי להציע רק את calc.py."),
                         review("CONTINUE", findings=["calc.py was written but has a syntax error (line 1)."],
                                next_instruction="Fix the syntax error in calc.py: add the missing ':' after def add(a, b).", question="",
                                user_summary_he="הקובץ נכתב, אבל בדיקת התחביר נכשלה. ביקשתי תיקון."),
                         review("COMPLETE", findings=["calc.py written, matches the proposal, syntax ok, add() defined. Not executed."],
                                next_instruction="", question="",
                                user_summary_he="הקובץ נכתב, זהה להצעה ותקין תחבירית. הקוד לא הורץ, ולכן האישור הוא על סמך בדיקה סטטית בלבד.")]}


def run_demo(cfg, out=print, viewer=None, delay: float = 0.0, on_ready=None, he_summaries: bool = False, proposals: bool = False) -> int:
    from agents import make_mock_agents
    from orchestrator import EXIT, Orchestrator, resolve_runs_dir
    tmp = Path(tempfile.mkdtemp(prefix="orch-demo-"))
    try:
        ws = make_workspace(tmp / "workspace")
        cfg = dict(cfg, verify_command=[sys.executable, "-m", "unittest", "discover", "-s", "tests"])
        out(f"demo workspace (temporary, mock agents only): {ws}")
        spec = None
        if proposals:
            from proposals import make_file_spec
            from safety import Policy
            spec = make_file_spec(["calc.py"], [], ["add"], Policy(ws, cfg))
            scenario, task = proposal_scenario(), PROPOSAL_TASK
        elif he_summaries:
            scenario, task = he_summary_scenario(), HE_SUMMARY_TASK
        else:
            scenario, task = rejection_scenario(), "Implement calc.add and verify all tests"
        orc = Orchestrator(cfg, ws, resolve_runs_dir(cfg), *make_mock_agents(scenario, delay), out=out, viewer=viewer, file_spec=spec)
        if on_ready:
            on_ready(orc.task_id)  # e.g. start the browser viewer BEFORE the first message
        st = orc.start(task)
        out(f"result: {st['status']} - transcript: {resolve_runs_dir(cfg) / st['task_id'] / 'transcript.jsonl'}")
        out(f"replay it with: python orchestrator.py replay --task-id {st['task_id']} --delay 0")
        return EXIT[st["status"]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_demo_claude(cfg, out=print, viewer=None, delay: float = 0.0, on_ready=None) -> int:
    """Mock Codex + the REAL Claude adapter driven by a SIMULATED Claude CLI (no model is called)."""
    from agents import make_mock_agents
    from claude_adapter import ClaudeCli, ClaudeExecutor
    from orchestrator import EXIT, Orchestrator, resolve_runs_dir
    from sim_claude import SimClaude
    tmp = Path(tempfile.mkdtemp(prefix="orch-demo-"))
    try:
        ws = make_workspace(tmp / "workspace")
        exe = tmp / "claude.cmd"
        exe.write_text("simulated", encoding="utf-8")
        cfg = dict(cfg, verify_command=[sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                   claude=dict(cfg["claude"], executable=str(exe)))
        steps = [{"ops": [{"op": "write", "path": "calc.py", "content": BAD}],
                  "report": {"summary": "Created calc.py with add(); ran the unit tests.", "changed_files": ["calc.py"],
                             "commands_run": ["python -m unittest discover -s tests"], "remaining_issues": []}},
                 {"ops": [{"op": "write", "path": "calc.py", "content": GOOD}],
                  "report": {"summary": "Fixed add() to return a + b; tests pass.", "changed_files": ["calc.py"],
                             "commands_run": ["python -m unittest discover -s tests"], "remaining_issues": []}}]
        sim = SimClaude(steps)
        planner, _, reviewer = make_mock_agents(rejection_scenario(), delay)
        out(f"demo workspace (temporary): {ws}")
        out("agents: Codex = MOCK planner/reviewer | Claude = REAL adapter talking to a SIMULATED CLI (no model is called)")
        orc = Orchestrator(cfg, ws, resolve_runs_dir(cfg), planner, ClaudeExecutor(ClaudeCli(cfg, runner=sim)), reviewer,
                           out=out, viewer=viewer)
        if on_ready:
            on_ready(orc.task_id)
        st = orc.start("Implement calc.add and verify all tests")
        out(f"result: {st['status']} - transcript: {resolve_runs_dir(cfg) / st['task_id'] / 'transcript.jsonl'}")
        out(f"claude session ids used: {sorted(sim.sessions)} (one session, resumed in cycle 2)")
        return EXIT[st["status"]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
