"""Conversation layer + live viewer tests (mock Codex / mock Claude, plus the Codex adapter with a fake CLI)."""
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import PY, UNITTEST_CMD, Base, ms, orch  # noqa: F401

import conversation as conv  # noqa: E402
import safety  # noqa: E402
import store  # noqa: E402
import viewer as vw  # noqa: E402
from agents import MockExecutor, MockReviewer, make_mock_agents  # noqa: E402
from test_codex_adapter import FakeRunner, CodexBase, base_events  # noqa: E402,F401
import codex_adapter as ca  # noqa: E402

GOOD, BAD = ms.GOOD, ms.BAD
SECRET = "sk-" + "ant-api03-" + "Q1w2E3r4" * 4
ENV_SECRET = "env-" + "secret-value-" + "4711"


def scen(executor, reviewer, planner=None):
    return {"planner": [ms.plan()] if planner is None else planner, "executor": executor, "reviewer": reviewer}


def frame_lines(lines):
    return [x for x in lines if x.startswith("+--") or x.startswith("| ")]


class ConvBase(Base):
    def setUp(self):
        super().setUp()
        self.shown: list[str] = []
        self.viewer = vw.Viewer(out=self.shown.append, width=80)

    def go(self, scenario, cfg_over=None, **kw):
        o, ad = self.make(scenario, cfg_over, viewer=self.viewer, **kw)
        st = o.start("implement calc.add and verify all tests")
        return o, ad, st

    def msgs(self, o):
        m, _, skipped = conv.read_transcript(o.run_dir / "transcript.jsonl")
        self.assertEqual(skipped, 0)
        return m

    @staticmethod
    def shape(msgs):
        return [(m["sender"], m["recipient"], m["kind"], m["cycle"]) for m in msgs]


class TestAttributionAndCapture(ConvBase):
    def test_sender_recipient_attribution_for_a_revision_cycle(self):
        o, _, st = self.go(ms.rejection_scenario())
        self.assertEqual(st["status"], "COMPLETE")
        self.assertEqual(self.shape(self.msgs(o)), [
            ("user", "codex", "task", 0), ("codex", "orchestrator", "plan", 0),
            ("codex", "claude", "instruction", 1), ("claude", "codex", "report", 1),
            ("orchestrator", "codex", "verification", 1), ("codex", "claude", "review", 1),
            ("codex", "claude", "instruction", 2), ("claude", "codex", "report", 2),
            ("orchestrator", "codex", "verification", 2), ("codex", "user", "review", 2),
            ("orchestrator", "user", "final", 2)])

    def test_messages_are_the_exact_text_exchanged(self):
        hebrew = "תוקן: add() מחזיר a + b ✓\nשורה שנייה"
        class RecEx(MockExecutor):
            raws = []
            def execute(self, ctx, fs):
                r = super().execute(ctx, fs); self.raws.append(r); return r
        class RecRv(MockReviewer):
            raws = []
            def review(self, ctx):
                r = super().review(ctx); self.raws.append(r); return r
        s = scen([ms.write_step(BAD, summary=hebrew), ms.write_step(GOOD)],
                 [ms.review("CONTINUE", findings=["x"], next_instruction="תקן: " + "return a + b " + "בבקשה"), ms.review("COMPLETE")])
        pl, _, _ = make_mock_agents(s)
        ex, rv = RecEx(s["executor"]), RecRv(s["reviewer"])
        o = orch.Orchestrator(self.cfg(), self.ws, self.runs, pl, ex, rv, out=self.shown.append, viewer=self.viewer)
        o.start("משימה: implement calc.add")
        m = self.msgs(o)
        by = {(x["kind"], x["cycle"]): x for x in m}
        self.assertEqual(by[("task", 0)]["content"], "משימה: implement calc.add")
        self.assertEqual(by[("report", 1)]["content"], RecEx.raws[0])
        self.assertEqual(json.loads(by[("report", 1)]["content"])["summary"], hebrew)
        self.assertEqual(by[("review", 1)]["content"], RecRv.raws[0])
        self.assertEqual(by[("review", 2)]["content"], RecRv.raws[1])
        self.assertEqual(by[("instruction", 2)]["content"], json.loads(RecRv.raws[0])["next_instruction"])
        self.assertEqual(by[("instruction", 1)]["content"], ms.plan()["instruction"])
        self.assertEqual(json.loads(by[("verification", 1)]["content"])["verification"]["status"], "failed")

    def test_invalid_attribution_is_rejected(self):
        t = conv.Transcript(self.base / "t.jsonl")
        for args in (("robot", "claude", "task"), ("codex", "nobody", "task"), ("codex", "claude", "gossip")):
            with self.assertRaises(ValueError):
                t.add(*args, content="x")

    def test_orchestrator_authored_instruction_is_not_attributed_to_codex(self):
        # reviewer says COMPLETE but verification fails -> the corrective instruction comes from the orchestrator
        s = scen([ms.write_step(BAD), ms.write_step(GOOD)], [ms.review("COMPLETE"), ms.review("COMPLETE")])
        o, _, st = self.go(s)
        ins = [x for x in self.msgs(o) if x["kind"] == "instruction"]
        self.assertEqual([(x["sender"], x["cycle"]) for x in ins], [("codex", 1), ("orchestrator", 2)])
        notice = [x for x in self.msgs(o) if x["kind"] == "notice"]
        self.assertTrue(any("COMPLETE rejected" in x["content"] for x in notice))


class TestOrderAndRealtime(ConvBase):
    def test_chronological_order_and_multiple_revision_cycles(self):
        steps = [ms.write_step(BAD + f"# {i}\n") for i in range(2)] + [ms.write_step(GOOD)]
        revs = [ms.review("CONTINUE", next_instruction=f"attempt {i} is wrong, return a + b") for i in range(2)] + [ms.review("COMPLETE")]
        o, _, st = self.go(scen(steps, revs))
        m = self.msgs(o)
        self.assertEqual([x["seq"] for x in m], list(range(1, len(m) + 1)))
        self.assertEqual([x["ts"] for x in m], sorted(x["ts"] for x in m))
        self.assertEqual([x["cycle"] for x in m], sorted(x["cycle"] for x in m))
        self.assertEqual(sorted({x["cycle"] for x in m}), [0, 1, 2, 3])
        shown_order = [int(re.search(r"#(\d+)", l).group(1)) for l in self.shown if l.startswith("+-- #")]
        self.assertEqual(shown_order, [x["seq"] for x in m])
        c2 = vw.select(m, cycle=2)
        self.assertEqual([x["kind"] for x in c2], ["instruction", "report", "verification", "review"])
        self.assertIn("attempt 0", c2[0]["content"])

    def test_messages_are_displayed_as_they_happen_not_at_the_end(self):
        shown = self.shown
        class SnoopEx(MockExecutor):
            seen = None
            def execute(self, ctx, fs):
                SnoopEx.seen = list(shown); return super().execute(ctx, fs)
        class SnoopRv(MockReviewer):
            seen = None
            def review(self, ctx):
                SnoopRv.seen = list(shown); return super().review(ctx)
        s = ms.success_scenario()
        pl, _, _ = make_mock_agents(s)
        o = orch.Orchestrator(self.cfg(), self.ws, self.runs, pl, SnoopEx(s["executor"]), SnoopRv(s["reviewer"]),
                              out=shown.append, viewer=self.viewer)
        o.start("t")
        before_exec = "\n".join(SnoopEx.seen)
        self.assertIn("CODEX -> CLAUDE  [instruction]", before_exec)
        self.assertNotIn("[report]", before_exec)
        before_review = "\n".join(SnoopRv.seen)
        self.assertIn("CLAUDE -> CODEX  [report]", before_review)
        self.assertIn("ORCHESTRATOR -> CODEX  [verification]", before_review)
        self.assertNotIn("[review]", before_review)

    def test_broken_viewer_never_breaks_the_task(self):
        def boom(rec): raise RuntimeError("display crashed")
        o2 = orch.Orchestrator(self.cfg(), self.ws, self.runs, *make_mock_agents(ms.success_scenario()), out=self.shown.append)
        o2.viewer = None
        o2._open = (lambda orig: (lambda: (orig(), o2.transcript.listeners.append(boom))))(o2._open)
        st = o2.start("t")
        self.assertEqual(st["status"], "COMPLETE")
        self.assertEqual(len(self.msgs(o2)), 7)


class TestCodexStreaming(CodexBase):
    def test_streamed_events_show_progress_before_the_reply_and_hide_reasoning(self):
        lines = []
        v = vw.Viewer(out=lines.append, width=80)
        events = base_events(json.dumps(ms.plan()))
        events.insert(2, {"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "HIDDEN-THOUGHT-123"}})
        runner = FakeRunner([{"final": json.dumps(ms.plan()), "events": events}, self.REVIEW_OK])
        cfg = self.cfg(codex=self.codex_cfg)
        cli = ca.CodexCli(cfg, base_dir=self.proj, runner=runner)
        _, ex, _ = make_mock_agents({"executor": [ms.write_step(GOOD)]})
        o = orch.Orchestrator(cfg, self.ws, self.runs, ca.CodexPlanner(cli), ex, ca.CodexReviewer(cli), out=lines.append, viewer=v)
        st = o.start("task")
        self.assertEqual(st["status"], "COMPLETE")
        text = "\n".join(lines)
        i_act = next(i for i, l in enumerate(lines) if "codex: session started" in l)
        i_work = next(i for i, l in enumerate(lines) if "codex: working (gpt-6-sol, effort high)" in l)
        i_plan = next(i for i, l in enumerate(lines) if "CODEX -> ORCHESTRATOR  [plan]" in l)
        self.assertLess(i_act, i_work); self.assertLess(i_work, i_plan)
        self.assertIn("reply received", text)
        self.assertNotIn("HIDDEN-THOUGHT-123", text)
        stored = (o.run_dir / "transcript.jsonl").read_text(encoding="utf-8") + (o.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("HIDDEN-THOUGHT-123", stored)
        self.assertNotIn("activity", (o.run_dir / "transcript.jsonl").read_text(encoding="utf-8").replace('"kind"', ""))
        self.assertIn("activity", [json.loads(l)["event"] for l in (o.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()])


class TestStreamingProcess(Base):
    def test_lines_arrive_while_the_process_is_still_running(self):
        code = "import time\nprint('first', flush=True)\ntime.sleep(1.5)\nprint('second', flush=True)\n"
        got, t0 = [], time.time()
        r = safety.run_process([PY, "-c", code], cwd=self.base, env=os.environ.copy(), timeout=30,
                               on_stdout_line=lambda l: got.append((time.time() - t0, l)))
        total = time.time() - t0
        self.assertEqual([l for _, l in got], ["first", "second"])
        self.assertLess(got[0][0], total - 1.0, "first line must be delivered long before the process ends")
        self.assertEqual(r.stdout.split(), ["first", "second"])

    def test_streaming_mode_still_enforces_timeout_and_cancel(self):
        sleeper = [PY, "-c", "import time; print('x', flush=True); time.sleep(60)"]
        with self.assertRaises(safety.ProcessTimeout):
            safety.run_process(sleeper, cwd=self.base, env=os.environ.copy(), timeout=2, on_stdout_line=lambda l: None)
        t0 = time.time()
        with self.assertRaises(safety.ProcessCancelled):
            safety.run_process(sleeper, cwd=self.base, env=os.environ.copy(), timeout=60,
                               cancel_check=lambda: time.time() - t0 > 1, on_stdout_line=lambda l: None)

    def test_exception_in_callback_is_ignored(self):
        def bad(l): raise ValueError("x")
        r = safety.run_process([PY, "-c", "print('ok')"], cwd=self.base, env=os.environ.copy(), timeout=20, on_stdout_line=bad)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"))


class TestRedaction(ConvBase):
    def test_secrets_never_reach_transcript_or_screen(self):
        leak = f"{SECRET} OPENAI_API_KEY=abc123def456ghi789 Bearer {'z' * 30} {ENV_SECRET}"
        s = scen([ms.write_step(GOOD, summary=leak)], [ms.review("COMPLETE", findings=[leak])], planner=[ms.plan(instruction="go " + leak)])
        with mock.patch.dict(os.environ, {"SOME_SERVICE_TOKEN": ENV_SECRET}):
            o, ad = self.make(s, viewer=self.viewer)
            o.start("task " + leak)
        blob = (o.run_dir / "transcript.jsonl").read_text(encoding="utf-8") + "\n".join(self.shown)
        for secret in (SECRET, "abc123def456ghi789", "z" * 30, ENV_SECRET):
            self.assertNotIn(secret, blob)
        self.assertIn("[REDACTED]", blob)


class TestReadability(ConvBase):
    REC = {"seq": 7, "ts": "2026-10-12T01:02:03+00:00", "cycle": 2, "sender": "codex", "recipient": "claude",
           "kind": "instruction", "delivery": "complete", "content": ""}

    def fmt(self, content, width=60, color=False):
        return vw.format_message(dict(self.REC, content=content), width, color)

    def test_width_wrapping_and_no_ansi_without_color(self):
        long = "x" * 200 + " " + "word " * 40
        lines = self.fmt(long + "\n" + json.dumps({"output_tail": "line1\nline2", "n": 1, "ok": True, "empty": []}))
        self.assertTrue(all(len(l) <= 60 for l in lines), [len(l) for l in lines if len(l) > 60])
        self.assertFalse(any("\x1b" in l for l in lines))
        self.assertTrue(lines[0].startswith("+-- #7  cycle 2  2026-10-12 01:02:03Z  CODEX -> CLAUDE  [instruction]"[:60]))
        self.assertEqual(lines[-1], "+--")
        self.assertTrue(all(l.isascii() for l in lines if l.startswith("+--")))

    def test_json_is_shown_as_readable_blocks(self):
        body = "\n".join(self.fmt(json.dumps({"verification": {"status": "failed", "output_tail": "Ran 2 tests\nFAILED"}, "files": ["a.py"]}), 100))
        self.assertIn("output_tail: |", body)
        self.assertIn("  Ran 2 tests", body.replace("|", "").replace("   ", "  "))
        self.assertNotIn("\\n", body)

    def test_agent_text_cannot_inject_terminal_escapes(self):
        lines = self.fmt("\x1b[31mred\x1b[0m \x07bell \x1b]0;pwned\x07 ok")
        self.assertFalse(any(("\x1b" in l) or ("\x07" in l) for l in lines))
        self.assertTrue(any("ok" in l for l in lines))

    def test_color_is_only_applied_to_the_header(self):
        lines = self.fmt("plain body", color=True)
        self.assertIn("\x1b[", lines[0]); self.assertNotIn("\x1b", "".join(lines[1:]))

    def test_unicode_and_empty_content_do_not_crash(self):
        for c in ("שלום עולם ✓", "", "   ", "{}", "[]", "null", "12"):
            self.assertTrue(self.fmt(c))

    def test_activity_line_format(self):
        s = vw.format_activity({"ts": "2026-10-12T01:02:03+00:00", "source": "codex", "text": "working"}, 80)
        self.assertEqual(s, "  .. 01:02:03Z codex: working")


class TestPersistenceReplayAndCli(ConvBase):
    def setUp(self):
        super().setUp()
        self.cfgfile = self.base / "config.json"
        self.cfgfile.write_text(json.dumps({"runs_dir": str(self.runs), "verify_command": UNITTEST_CMD}), encoding="utf-8")
        self.cli_out: list[str] = []

    def cli(self, *args):
        return orch.main(["--config", str(self.cfgfile), *args], out=self.cli_out.append)

    def test_replay_equals_the_live_view_and_uses_the_given_delay(self):
        o, _, st = self.go(ms.rejection_scenario())
        live = frame_lines(self.shown)
        replayed: list[str] = []
        sleeps: list[float] = []
        n = vw.replay(o.run_dir / "transcript.jsonl", vw.Viewer(out=replayed.append, width=80), delay=0.25, sleep=sleeps.append)
        self.assertEqual(frame_lines(replayed), live)
        self.assertEqual((n, sleeps), (11, [0.25] * 10))

    def test_conversation_commands_after_the_run(self):
        o, _, st = self.go(ms.rejection_scenario())
        self.assertEqual(self.cli("status"), 0)
        self.assertTrue(any("conversation: 11 messages (last #11)" in x for x in self.cli_out))
        self.cli_out.clear()
        self.assertEqual(self.cli("conversation", "--message", "4"), 0)
        text = "\n".join(self.cli_out)
        self.assertIn("#4", text); self.assertIn("CLAUDE -> CODEX  [report]", text); self.assertNotIn("#5", text)
        self.cli_out.clear()
        self.assertEqual(self.cli("conversation", "--cycle", "2", "--json"), 0)
        recs = [json.loads(x) for x in self.cli_out]
        self.assertEqual([r["kind"] for r in recs], ["instruction", "report", "verification", "review", "final"])
        self.assertEqual(recs[0]["content"], "add() returns a - b; it must return a + b.")
        self.cli_out.clear()
        self.assertEqual(self.cli("replay", "--delay", "0", "--cycle", "1"), 0)
        self.assertIn("--- end of replay: 4 message(s) ---", self.cli_out)
        self.assertEqual(self.cli("conversation", "--message", "999"), 1)

    def cli_watch_done(self):
        """`watch` on a task that already finished prints its messages and returns immediately."""
        self.go(ms.success_scenario())
        self.cli_out.clear()
        rc = self.cli("watch", "--poll", "0.01")
        self.assertEqual(len([x for x in self.cli_out if x.startswith("+-- #")]), 7)
        return rc

    def test_watch_follows_new_messages_and_finishes_when_the_task_ends(self):
        path = self.base / "t.jsonl"
        tr = conv.Transcript(path)
        tr.add("user", "codex", "task", "first", 0)
        out: list[str] = []
        steps = {"n": 0}
        def fake_sleep(_):
            steps["n"] += 1
            if steps["n"] == 1:
                tr.add("codex", "claude", "instruction", "second", 1)
            if steps["n"] == 2:
                tr.add("claude", "codex", "report", "third", 1)
        active = {"v": True}
        def is_active():
            if steps["n"] >= 3: active["v"] = False
            return active["v"]
        shown = vw.watch(path, vw.Viewer(out=out.append, width=80), is_active, poll=0.01, sleep=fake_sleep)
        self.assertEqual(shown, 3)
        self.assertEqual([l for l in out if l.startswith("+-- #")][-1].split("  ")[-1], "[report]")
        self.assertEqual(self.cli_watch_done(), 0)

    def test_truncated_and_garbled_transcript_lines_are_handled(self):
        path = self.base / "t.jsonl"
        tr = conv.Transcript(path)
        tr.add("user", "codex", "task", "one", 0)
        with open(path, "a", encoding="utf-8") as f:
            f.write("this is not json\n")
        tr.add("codex", "claude", "instruction", "two", 1)
        with open(path, "ab") as f:
            f.write(b'{"seq": 4, "sender": "claude", "recip')  # crash while writing
        msgs, off, skipped = conv.read_transcript(path)
        self.assertEqual(([m["content"] for m in msgs], skipped), (["one", "two"], 1))
        with open(path, "ab") as f:  # the writer finishes the line later
            f.write(b'ient": "codex", "kind": "report", "content": "three", "cycle": 1}\n')
        more, _, _ = conv.read_transcript(path, off)
        self.assertEqual([m["content"] for m in more], ["three"])
        buf: list[str] = []
        vw.replay(path, vw.Viewer(out=buf.append, width=80))
        self.assertTrue(any("unreadable transcript line" in x for x in buf))


class TestFailuresAndInterruptions(ConvBase):
    def test_malformed_agent_output_is_shown_exactly_and_the_task_ends_visibly(self):
        o, _, st = self.go(scen([{"raw": "I fixed it, trust me!"}], []))
        m = self.msgs(o)
        bad = [x for x in m if x["kind"] == "malformed_output"]
        self.assertEqual([(x["sender"], x["recipient"], x["content"]) for x in bad], [("claude", "orchestrator", "I fixed it, trust me!")])
        self.assertEqual((m[-1]["kind"], m[-1]["recipient"]), ("final", "user"))
        self.assertTrue(m[-1]["content"].startswith("FAILED: malformed_output"))
        ws2 = self.new_ws("ws2")
        o2, _ = self.make(scen([ms.write_step(GOOD)], [{"raw": json.dumps({"verdict": "DONE"})}]), ws=ws2, viewer=self.viewer)
        o2.start("t")
        self.assertIn(("codex", "orchestrator", "malformed_output"), [(x["sender"], x["recipient"], x["kind"]) for x in self.msgs(o2)])

    def test_executor_failure_is_reported_as_a_notice_not_as_a_claude_message(self):
        o, _, st = self.go(scen([{"error": "process", "message": "boom"}, ms.write_step(GOOD)], [ms.review("COMPLETE")]))
        self.assertEqual(st["status"], "FAILED")
        notes = [x for x in self.msgs(o) if x["kind"] == "notice"]
        self.assertEqual((notes[0]["sender"], notes[0]["recipient"]), ("orchestrator", "codex"))
        self.assertIn("executor failed (process): boom", notes[0]["content"])
        self.assertEqual([x for x in self.msgs(o) if x["sender"] == "claude" and x["cycle"] == 1], [])

    def test_stop_request_ends_with_control_and_final_messages(self):
        steps = [ms.write_step(BAD + f"# {i}\n") for i in range(3)]
        revs = [ms.review("CONTINUE", next_instruction="keep going, return a + b")] * 3
        o, _ = self.make(scen(steps, revs), viewer=self.viewer,
                         after_cycle=lambda orc, c: store.write_control(orc.run_dir, "stop") if c == 1 else None)
        st = o.start("t")
        m = self.msgs(o)
        self.assertEqual(st["status"], "STOPPED")
        self.assertEqual([(x["sender"], x["kind"]) for x in m[-2:]], [("user", "control"), ("orchestrator", "final")])
        self.assertEqual(m[-1]["content"], "STOPPED: user_stop")

    def test_pause_then_resume_continues_the_same_transcript(self):
        first = scen([ms.write_step(BAD)], [ms.review("CONTINUE", next_instruction="add() subtracts; return a + b")])
        o1, _ = self.make(first, viewer=self.viewer,
                          after_cycle=lambda orc, c: store.write_control(orc.run_dir, "pause") if c == 1 else None)
        self.assertEqual(o1.start("t")["status"], "PAUSED")
        n1 = len(self.msgs(o1))
        o2 = orch.Orchestrator(self.cfg(), self.ws, self.runs, *make_mock_agents(scen([ms.write_step(GOOD)], [ms.review("COMPLETE")], [])),
                               out=self.shown.append, task_id=o1.task_id, viewer=self.viewer)
        self.assertEqual(o2.resume()["status"], "COMPLETE")
        m = self.msgs(o2)
        self.assertEqual([x["seq"] for x in m], list(range(1, len(m) + 1)))
        self.assertEqual(m[n1 - 1]["kind"], "control")
        self.assertEqual(m[-1]["kind"], "final")

    def test_human_approval_request_and_decision_are_in_the_conversation(self):
        (self.ws / "old.txt").write_text("legacy", encoding="utf-8")
        delete = {"ops": [{"op": "delete", "path": "old.txt"}, {"op": "write", "path": "calc.py", "content": GOOD}]}
        o1, _ = self.make(scen([delete], []), viewer=self.viewer)
        self.assertEqual(o1.start("t")["status"], "HUMAN_REQUIRED")
        req = [x for x in self.msgs(o1) if x["kind"] == "approval_request"]
        self.assertEqual((req[0]["sender"], req[0]["recipient"]), ("orchestrator", "user"))
        self.assertIn("approval_required:a1", req[0]["content"]); self.assertIn("old.txt", req[0]["content"])
        orch.resolve_approval(self.runs, o1.task_id, "a1", True)
        o2 = orch.Orchestrator(self.cfg(), self.ws, self.runs, *make_mock_agents(scen([delete], [ms.review("COMPLETE")], [])),
                               out=self.shown.append, task_id=o1.task_id, viewer=self.viewer)
        o2.resume()
        kinds = [(x["sender"], x["kind"]) for x in self.msgs(o2)]
        self.assertLess(kinds.index(("orchestrator", "approval_request")), kinds.index(("user", "approval_decision")))
        dec = [x for x in self.msgs(o2) if x["kind"] == "approval_decision"][0]
        self.assertEqual(dec["content"], "APPROVED a1: delete old.txt")
        # denial path
        ws2 = self.new_ws("ws2"); (ws2 / "old.txt").write_text("x", encoding="utf-8")
        o3, _ = self.make(scen([{"ops": [{"op": "delete", "path": "old.txt"}]}], []), ws=ws2, viewer=self.viewer)
        o3.start("t")
        orch.resolve_approval(self.runs, o3.task_id, "a1", False)
        tail = self.msgs(o3)[-2:]
        self.assertEqual([(x["kind"], x["content"]) for x in tail],
                         [("approval_decision", "DENIED a1: delete old.txt"), ("final", "FAILED: approval_denied")])

    def test_quiet_mode_still_saves_the_full_transcript(self):
        cfgfile = self.base / "config.json"
        cfgfile.write_text(json.dumps({"runs_dir": str(self.runs), "verify_command": UNITTEST_CMD}), encoding="utf-8")
        scn = self.base / "s.json"; scn.write_text(json.dumps(ms.success_scenario()), encoding="utf-8")
        out: list[str] = []
        rc = orch.main(["--config", str(cfgfile), "run", "--project", str(self.ws), "--task", "t", "--scenario", str(scn), "--quiet"], out=out.append)
        self.assertEqual(rc, 0)
        self.assertEqual([l for l in out if l.startswith("+-- #")], [])
        t = next(self.runs.glob("*/transcript.jsonl"))
        self.assertEqual(len(conv.read_transcript(t)[0]), 7)


if __name__ == "__main__":
    unittest.main()
