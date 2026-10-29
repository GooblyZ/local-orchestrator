"""Owner-first UX: plain-Hebrew reasons and states, honest research labels, the current result of a resumed task, 'needs you'
in the task list, Codex's question shown to the owner, and the page's own logic exercised in node. Mock data only."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ms, orch  # noqa: F401
from test_goal import GoalBase, cont

import goal_mode as gm  # noqa: E402
import web_viewer as wv  # noqa: E402
from safety import Redactor  # noqa: E402
from store import atomic_write_json  # noqa: E402

HTML = (Path(orch.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
JS = max(re.findall(r"<script[^>]*>(.*?)</script>", HTML, re.S), key=len)


def js_function(name: str) -> str:
    """The source of a top-level `function name(...) {...}` of the page script (brace matching)."""
    i = JS.index(f"function {name}(")
    depth, j = 0, JS.index("{", i)
    for k in range(j, len(JS)):
        depth += {"{": 1, "}": -1}.get(JS[k], 0)
        if depth == 0:
            return JS[i:k + 1]
    raise AssertionError(name)


def js_const(name: str) -> str:
    return re.search(rf"^const {name} = .*?;$", JS, re.M | re.S).group(0)


def run_js(body: str):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    src = "\n".join([js_const("STATUS_HE"), js_const("VERDICT"),
                     "const AG = {codex: {name: 'Codex'}, claude: {name: 'Claude'}};",
                     "const S = {messages: [], status: null};", "function obj(m) { try { return JSON.parse(m.content); } catch (e) { return null; } }",
                     *[js_function(n) for n in ("humanReason", "ownerState", "currentFinal", "verShort", "summaryStale", "relTime", "factsAfter")],
                     body])
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(src)
    try:
        r = subprocess.run([node, f.name], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(f.name).unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr[-1500:]
    return json.loads(r.stdout)


class TestPageLogic(unittest.TestCase):
    def test_reasons_are_plain_hebrew_and_never_lose_the_raw_code(self):
        out = run_js("""console.log(JSON.stringify([
          "budget_reached: time 180/180 min (reached during Claude's call; it continues after resume)", "budget_reached: claude calls 40/40",
          "reviewer_requested_human", "stalled: the same instruction was repeated 3 times", "internal_error: PermissionError: x",
          "malformed_output: field 'summary' invalid", "research report verified for structure and evidence presence (not for truth); verdict PIVOT",
          "something_new: raw"].map(r => humanReason({stop_reason: r}))));""")
        self.assertEqual(out[0], "נגמר זמן העבודה שאישרת: 180 מתוך 180 דקות. העבודה שבוצעה נשמרה.")
        self.assertEqual(out[1], "נגמר מספר הפניות ל-Claude שאישרת: 40 מתוך 40. העבודה שבוצעה נשמרה.")
        self.assertIn("Codex עצר ושאל", out[2])
        self.assertIn("בלי התקדמות מדידה", out[3])
        self.assertIn("לא בגלל הסוכנים", out[4])
        self.assertIn("לא בגלל התוכן", out[5])
        self.assertIn("לא את נכונות התוכן", out[6], "a completed research task never claims the findings were verified")
        self.assertEqual(out[7], "something_new: raw", "an unknown code is shown as it is, not hidden")

    def test_the_owner_state_answers_do_i_need_to_act(self):
        out = run_js("""console.log(JSON.stringify([
          {run_state: "active", status: "RUNNING"}, {run_state: "interrupted", status: "RUNNING"},
          {run_state: "saved", status: "PAUSED", stop_reason: "budget_reached: time 120/120 min"},
          {run_state: "saved", status: "HUMAN_REQUIRED", stop_reason: "reviewer_requested_human"},
          {run_state: "saved", status: "HUMAN_REQUIRED", stop_reason: "permission_denied: x", pending_permissions: [{tool: "Bash"}]},
          {run_state: "saved", status: "COMPLETE", stop_reason: "acceptance verified: tests passed and reviewer approved"},
          {run_state: "saved", status: "INCONCLUSIVE", stop_reason: "research finished without sufficient evidence"},
          {run_state: "saved", status: "FAILED", kind: "research", plan: "p", stop_reason: "malformed_output: x"},
          {run_state: "saved", status: "FAILED", stop_reason: "reviewer_declared_failure: y"}].map(s => { const o = ownerState(s); return [o.tone, o.need, o.title]; })));""")
        tones = [(t, n) for t, n, _ in out]
        self.assertEqual(tones, [("live", False), ("need", True), ("need", True), ("need", True), ("need", True), ("ok", False),
                                 ("mid", False), ("need", True), ("bad", False)])
        self.assertEqual(out[4][2], "Claude מחכה לאישור שלך")
        self.assertIn("נגמר התקציב", out[2][2])

    def test_an_earlier_failure_is_not_the_result_of_a_task_that_continued(self):
        out = run_js("""const m = (seq, kind, content) => ({seq, kind, content: content || "{}"});
          const resumed = [m(1, "plan"), m(2, "review"), m(3, "final", "FAILED: malformed_output: x"), m(4, "instruction"), m(5, "review")];
          const ended = [m(1, "plan"), m(2, "review"), m(3, "final", "COMPLETE: done")];
          console.log(JSON.stringify([currentFinal(resumed), currentFinal(ended) && currentFinal(ended).seq]));""")
        self.assertEqual(out, [None, 3])

    def test_research_cycles_are_not_labelled_as_failed_tests(self):
        out = run_js("""console.log(JSON.stringify([verShort({mode: "research", status: "research_passed"}), verShort({mode: "research", status: "research_missing"}),
          verShort({status: "failed"})]));""")
        self.assertEqual(out[0], ["yes", "הדוח נבדק (מבנה בלבד)"])
        self.assertEqual(out[1][1], "אין עדיין דוח")
        self.assertEqual(out[2], ["bad", "בדיקות הורצו ונכשלו"], "code tasks keep their meaning")

    def test_a_summary_is_stale_only_after_real_work_and_since_last_visit_lists_system_facts(self):
        out = run_js("""const sm = {author: "codex", cycle: 2, after_seq: 10};
          S.messages = [{seq: 11, cycle: 2, kind: "review", content: '{"verdict":"CONTINUE"}'}, {seq: 12, cycle: 2, kind: "approval_request", content: "x"}];
          const pausedOnly = summaryStale(sm, {});
          S.messages.push({seq: 13, cycle: 3, kind: "instruction", content: "i"}, {seq: 14, cycle: 3, kind: "report", content: "{}"},
                          {seq: 15, cycle: 3, kind: "review", content: '{"verdict":"COMPLETE"}'});
          console.log(JSON.stringify([pausedOnly, summaryStale(sm, {}), factsAfter(12), relTime(Date.now() - 3 * 3600 * 1000)]));""")
        self.assertFalse(out[0], "its own review and a pause are not new work")
        self.assertTrue(out[1])
        self.assertEqual(out[2], ["סבב 3: Claude סיים צעד עבודה ושלח דיווח", "סבב 3: Codex בדק ואישר"])
        self.assertEqual(out[3], "לפני 3 שעות")


class TestPageContent(unittest.TestCase):
    def test_the_overview_and_plain_decisions_are_on_the_page(self):
        for needed in ("function overviewCard(", "למה שנדרש ממך ↓", "מאז הביקור האחרון שלך", "נתוני מערכת — לא סיכום של סוכן",
                       "מה הסוכנים מדווחים", "מה הושג: ", "מה עדיין פתוח: ", "מה הם מציעים: ",
                       "בהמשך: הסוכנים ימשיכו מאותה נקודה", "בסיום: המשימה נעצרת", "Codex שואל: ",
                       "אישור: Claude יוכל לבצע את הפעולה הזו במשימה הזו בלבד", "ממתין לך · ",
                       "כתוב מה תרצה שיעשו — משימת קוד או מחקר", ".layout>aside#side{order:2}"):
            self.assertIn(needed, HTML)
        self.assertNotIn("כתוב משימת תכנות", HTML)
        self.assertNotIn("כל הסעיפים והראיות הנדרשות קיימים", HTML, "no overclaim about research checks")


class TestTaskListAndStatus(GoalBase):
    def test_the_list_shows_who_needs_the_owner_clean_titles_and_honest_states(self):
        task = "# AUTONOMOUS DEEP RESEARCH MISSION\n## details"
        o, st = self.goal_run([ms.write_step(ms.GOOD)], [ms.review("HUMAN_REQUIRED", question="לאשר ניסוי של 48 שעות?")], task=task)
        rows = {t["id"]: t for t in wv.list_tasks(self.runs, Redactor())}
        row = rows[o.task_id]
        self.assertEqual(row["short_title"], "AUTONOMOUS DEEP RESEARCH MISSION")
        self.assertTrue(row["needs_owner"])
        self.assertEqual(row["kind"], "code")
        status = wv.RunReader(o.run_dir, Redactor()).status()
        self.assertEqual(status["pending_question"], "לאשר ניסוי של 48 שעות?", "the owner sees Codex's actual question")
        s = json.loads(o.state_path.read_text(encoding="utf-8"))
        s.update(status="RUNNING", stop_reason=None)  # a process that died while running
        atomic_write_json(o.state_path, s)
        row = {t["id"]: t for t in wv.list_tasks(self.runs, Redactor())}[o.task_id]
        self.assertEqual((row["run_state"], row["needs_owner"]), ("interrupted", True), "never listed as running")

    def test_a_completed_task_does_not_need_the_owner(self):
        o, st = self.goal_run([ms.write_step(ms.GOOD)], [ms.review("COMPLETE")])
        self.assertFalse({t["id"]: t for t in wv.list_tasks(self.runs, Redactor())}[o.task_id]["needs_owner"])


if __name__ == "__main__":
    unittest.main()
