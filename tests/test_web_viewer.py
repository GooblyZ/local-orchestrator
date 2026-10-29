"""Browser live viewer: server security, exact content, live streaming, phase events, launch integration."""
import http.client
import os
import json
import re
import socket
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, UNITTEST_CMD, Base, ms, orch  # noqa: F401

import conversation as conv  # noqa: E402
import mock_scenarios  # noqa: E402
import web_viewer as wv  # noqa: E402
from config import ConfigError, DEFAULT_CONFIG, load_config  # noqa: E402

FAKE_KEY = "sk-" + "ant-api03-" + "Mn4Pq5Rs" * 4
EVIL = '<img src=x onerror="window.__pwn=1"><script>window.__pwn=2</script> ' + FAKE_KEY


class WebBase(Base):
    def setUp(self):
        super().setUp()
        self.runs.mkdir(parents=True, exist_ok=True)
        self.srv = wv.ViewerServer(self.runs, 0).start()
        self.addCleanup(self.srv.stop)

    def req(self, path, method="GET", token=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        sep = "&" if "?" in path else "?"
        full = path + (f"{sep}t={self.srv.token}" if token is True else f"{sep}t={token}" if token else "")
        c.request(method, full, headers={"Host": host} if host else {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def jget(self, path, **kw):
        st, hd, body = self.req(path, **kw)
        return st, json.loads(body) if body and hd.get("Content-Type", "").startswith("application/json") else body

    def run_task(self, scenario=None):
        o, _ = self.make(scenario or ms.rejection_scenario())
        o.start("implement calc.add and verify all tests")
        return o

    def stream(self, task, after=0, eafter=0):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=6)
        c.request("GET", f"/stream?t={self.srv.token}&task={task}&after={after}&eafter={eafter}")
        return c, c.getresponse()

    @staticmethod
    def next_event(resp):
        ev, data = None, None
        while True:
            line = resp.fp.readline().decode("utf-8")
            if not line:
                return None, None
            line = line.rstrip("\n")
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
            elif line == "" and ev:
                return ev, data


class TestSecurity(WebBase):
    def test_binds_to_loopback_only(self):
        self.assertEqual(self.srv.httpd.server_address[0], "127.0.0.1")
        self.assertGreater(self.srv.port, 0)
        self.assertTrue(self.srv.url().startswith("http://127.0.0.1:"))

    def test_token_and_host_are_required(self):
        self.assertEqual(self.req("/api/tasks", token=False)[0], 403)
        self.assertEqual(self.req("/api/tasks", token="wrong-token")[0], 403)
        self.assertEqual(self.req("/api/tasks")[0], 200)
        self.assertEqual(self.req("/api/tasks", host="evil.example.com")[0], 403)   # DNS-rebinding style request
        self.assertEqual(self.req("/api/tasks", host=f"localhost:{self.srv.port}")[0], 200)
        self.assertEqual(self.req("/api/tasks", host=f"127.0.0.1:{self.srv.port + 1}")[0], 403)

    def test_viewer_is_read_only(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.req("/api/tasks", method=m)[0], 405, m)

    def test_task_ids_cannot_escape_the_runs_directory(self):
        self.run_task()
        for bad in ("../x", "..%2Fx", "/etc/passwd", "C:\\Windows", "a/b", "", "." * 3 + "/..", "x" * 300):
            self.assertEqual(self.req(f"/api/snapshot?task={bad}")[0], 404, bad)
        self.assertEqual(self.req("/api/snapshot?task=does-not-exist")[0], 404)
        self.assertEqual(self.req("/nothing")[0], 404)

    def test_page_headers_csp_and_hygiene(self):
        st, hd, body = self.req("/")
        html = body.decode("utf-8")
        self.assertEqual(st, 200)
        csp = hd["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp); self.assertIn("connect-src 'self'", csp); self.assertIn("frame-ancestors 'none'", csp)
        nonce = re.search(r"nonce-([\w-]+)", csp).group(1)
        self.assertEqual(html.count(f'nonce="{nonce}"'), 2)
        self.assertNotIn("__NONCE__", html); self.assertNotIn("__TOKEN__", html)
        self.assertIn(self.srv.token, html)
        self.assertEqual(hd["Cache-Control"], "no-store"); self.assertEqual(hd["X-Content-Type-Options"], "nosniff")

    def test_page_source_never_interprets_agent_text_and_loads_nothing_external(self):
        src = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write", "new Function"):
            self.assertNotIn(bad, src)
        self.assertEqual(re.findall(r"https?://", src), [], "the page must not reference any external URL")
        self.assertNotRegex(src, r"<script[^>]+src=")
        for needed in ("EventSource", 'lang="he" dir="rtl"', "תצוגת שיחה", "תצוגה טכנית", "הפעלה חוזרת", "העתק", "מקור", "הצג הכל",
                       "Codex", "מנהל ומבקר", "Claude", "מבצע", "המשימה שלך", "תוכנית העבודה", "הוראות ל-Claude", "הפתרון של Claude",
                       "ביקורת Codex", "ממתין לתשובה", "עובד כעת", "Codex ביקש סבב נוסף", "אושר בביקורת", "קוד מוצע — לא הורץ",
                       "בדיקות הורצו ועברו", "בדיקות הורצו ונכשלו", "בדיקה סטטית", "הסתיים", "ריצה פעילה", "שיחה שמורה", "user_summary_he"):
            self.assertIn(needed, src)
        self.assertIsNone(re.search(r"\bLIVE\b", src), "a completed run must never be labelled LIVE")


class TestContent(WebBase):
    def test_snapshot_is_exactly_the_saved_transcript(self):
        o = self.run_task()
        st, snap = self.jget(f"/api/snapshot?task={o.task_id}")
        disk, _, skipped = conv.read_transcript(o.run_dir / "transcript.jsonl")
        self.assertEqual((st, skipped, len(snap["messages"])), (200, 0, 11))
        for served, saved in zip(snap["messages"], disk):
            self.assertEqual({k: served[k] for k in ("seq", "ts", "cycle", "sender", "recipient", "kind", "delivery", "content")},
                             {k: saved[k] for k in ("seq", "ts", "cycle", "sender", "recipient", "kind", "delivery", "content")})
        s = snap["status"]
        self.assertEqual((s["status"], s["cycle"], s["max_cycles"], s["messages"], s["phase"]), ("COMPLETE", 2, 8, 11, None))
        self.assertTrue(s["title"].startswith("implement calc.add"))

    def test_tasks_list_and_unreadable_lines_are_tolerated(self):
        o = self.run_task()
        st, tl = self.jget("/api/tasks")
        self.assertEqual([t["id"] for t in tl["tasks"]], [o.task_id])
        self.assertEqual(tl["tasks"][0]["status"], "COMPLETE")
        with open(o.run_dir / "transcript.jsonl", "a", encoding="utf-8") as f:
            f.write("garbage line\n")
            f.write('{"seq": 99, "sender": "claude", "recip')  # crash while writing
        st, snap = self.jget(f"/api/snapshot?task={o.task_id}")
        self.assertEqual((st, len(snap["messages"])), (200, 11))

    def test_missing_files_mean_waiting_not_an_error(self):
        (self.runs / "fresh-task").mkdir()
        st, snap = self.jget("/api/snapshot?task=fresh-task")
        self.assertEqual((st, snap["messages"], snap["status"]["status"]), (200, [], "WAITING"))

    def test_runs_without_state_json_derive_title_and_status_from_the_transcript(self):
        d = self.runs / "harness-run"
        tr = conv.Transcript(d / "transcript.jsonl")
        tr.add("user", "codex", "task", "Implement is_leap_year", 0)
        tr.add("orchestrator", "user", "final", "COMPLETED: Codex verdict=COMPLETE. The code was NOT executed.", 1)
        st, snap = self.jget("/api/snapshot?task=harness-run")
        s = snap["status"]
        self.assertEqual((s["status"], s["title"], s["status_source"]), ("COMPLETED", "Implement is_leap_year", "derived from transcript"))

    def test_secrets_are_masked_again_and_html_is_served_as_inert_text(self):
        d = self.runs / "hostile"
        d.mkdir()
        rec = {"seq": 1, "ts": "2026-10-12T01:00:00+00:00", "cycle": 0, "sender": "claude", "recipient": "codex", "kind": "report",
               "delivery": "complete", "content": EVIL, "meta": {"delivered_prompt": "key " + FAKE_KEY}}
        (d / "transcript.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")  # written WITHOUT redaction on purpose
        st, body = self.req(f"/api/snapshot?task=hostile")[0], self.req("/api/snapshot?task=hostile")[2].decode("utf-8")
        self.assertNotIn(FAKE_KEY, body)
        msg = json.loads(body)["messages"][0]
        self.assertIn("[REDACTED]", msg["content"])
        self.assertIn('<img src=x onerror="window.__pwn=1"><script>', msg["content"], "HTML is data: returned unchanged, rendered with textContent")
        self.assertNotIn(FAKE_KEY, msg["meta"]["delivered_prompt"])


class TestRunState(WebBase):
    def test_completed_run_is_saved_not_active(self):
        o = self.run_task()
        st, snap = self.jget(f"/api/snapshot?task={o.task_id}")
        self.assertEqual(snap["status"]["run_state"], "saved")
        self.assertTrue(any(e["event"] == "agent_start" for e in snap["events"]), "technical view receives system events")
        self.assertTrue(all("seq" not in e for e in snap["events"]) and all("eseq" in e for e in snap["events"]))

    def test_running_state_is_active_only_while_the_task_holds_its_lock(self):
        import os, store
        d = self.runs / "running-task"
        d.mkdir()
        store.atomic_write_json(d / "state.json", {"task_id": "running-task", "status": "RUNNING", "workspace": str(self.ws),
                                                   "cycle": 1, "max_cycles": 8})
        conv.Transcript(d / "transcript.jsonl").add("user", "codex", "task", "t", 0)
        self.assertEqual(self.jget("/api/snapshot?task=running-task")[1]["status"]["run_state"], "interrupted")
        lock = store.acquire_lock(self.runs, self.ws, "running-task")   # held by this (alive) process
        try:
            self.assertEqual(self.jget("/api/snapshot?task=running-task")[1]["status"]["run_state"], "active")
        finally:
            store.release_lock(lock, "running-task")
        self.assertEqual(self.jget("/api/snapshot?task=running-task")[1]["status"]["run_state"], "interrupted")

    def test_prepared_test_awaiting_owner_is_never_shown_as_active(self):
        import store
        d = self.runs / "prepared"
        d.mkdir()
        store.atomic_write_json(d / "state.json", {"task_id": "prepared", "status": "AWAITING_OWNER", "workspace": str(self.ws)})
        conv.Transcript(d / "transcript.jsonl").add("user", "codex", "task", "t", 0)
        self.assertEqual(self.jget("/api/snapshot?task=prepared")[1]["status"]["run_state"], "awaiting_owner")
        src = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("ממתין לאישור המשתמש להתחלת הניסוי", src)

    def test_harness_runs_without_state_and_waiting_runs(self):
        (self.runs / "empty").mkdir()
        self.assertEqual(self.jget("/api/snapshot?task=empty")[1]["status"]["run_state"], "waiting")
        d = self.runs / "h"
        tr = conv.Transcript(d / "transcript.jsonl")
        tr.add("user", "codex", "task", "t", 0)
        self.assertEqual(self.jget("/api/snapshot?task=h")[1]["status"]["run_state"], "active")   # just written, no final yet
        tr.add("orchestrator", "user", "final", "COMPLETED: done", 1)
        self.assertEqual(self.jget("/api/snapshot?task=h")[1]["status"]["run_state"], "saved")
        old = time.time() - 3600
        d2 = self.runs / "old"
        conv.Transcript(d2 / "transcript.jsonl").add("user", "codex", "task", "t", 0)
        os.utime(d2 / "transcript.jsonl", (old, old))
        self.assertEqual(self.jget("/api/snapshot?task=old")[1]["status"]["run_state"], "interrupted")

    def test_saved_cli_envelopes_are_attached_redacted(self):
        d = self.runs / "env"
        conv.Transcript(d / "transcript.jsonl").add("user", "codex", "task", "t", 0)
        (d / "claude_envelope.json").write_text(json.dumps({"session_id": "s", "note": "key " + FAKE_KEY}), encoding="utf-8")
        st, body = self.req("/api/snapshot?task=env")[0], self.req("/api/snapshot?task=env")[2].decode("utf-8")
        att = json.loads(body)["attachments"]
        self.assertEqual([a["name"] for a in att], ["claude_envelope.json"])
        self.assertNotIn(FAKE_KEY, body)


class TestLive(WebBase):
    def test_new_messages_and_phase_changes_stream_without_refresh(self):
        d = self.runs / "live-task"
        tr = conv.Transcript(d / "transcript.jsonl")
        tr.add("user", "codex", "task", "first", 0)
        tr.add("codex", "claude", "instruction", "second", 1)
        conn, resp = self.stream("live-task", after=2)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type").split(";")[0], "text/event-stream")
        self.assertEqual(self.next_event(resp)[0], "hello")
        self.assertEqual(self.next_event(resp)[0], "status")            # initial status; nothing re-sent for seq <= after
        tr.add("claude", "codex", "report", "THIRD message, exact text <b>x</b>", 1)
        ev, data = self.next_event(resp)
        self.assertEqual((ev, data["seq"], data["content"], data["sender"]), ("message", 3, "THIRD message, exact text <b>x</b>", "claude"))
        log = d / "events.jsonl"
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"seq": 1, "ts": "2026-10-12T01:00:00+00:00", "event": "agent_start", "cycle": 1, "data": {"agent": "claude", "role": "executor"}}) + "\n")
            f.write(json.dumps({"seq": 2, "ts": "2026-10-12T01:00:01+00:00", "event": "activity", "cycle": 1, "data": {"ts": "2026-10-12T01:00:01+00:00", "source": "claude", "text": "started"}}) + "\n")
        seen = {}
        for _ in range(6):
            ev, data = self.next_event(resp)
            seen[ev] = data
            if ev == "status" and data["phase"]:
                break
        self.assertEqual(seen["status"]["phase"]["agent"], "claude")
        self.assertEqual(seen["activity"]["text"], "started")
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"seq": 3, "ts": "2026-10-12T01:00:05+00:00", "event": "agent_end", "cycle": 1, "data": {"agent": "claude", "role": "executor"}}) + "\n")
        got = []
        for _ in range(4):
            ev, data = self.next_event(resp)
            got.append((ev, data))
            if ev == "status":
                break
        self.assertIn(("sysevent", "agent_end"), [(e, d.get("event")) for e, d in got])  # technical view gets every event
        self.assertEqual((got[-1][0], got[-1][1]["phase"]), ("status", None))
        conn.close()

    def test_reconnect_with_after_does_not_resend_old_messages(self):
        o = self.run_task()
        conn, resp = self.stream(o.task_id, after=11, eafter=10_000)
        self.next_event(resp)
        ev, data = self.next_event(resp)
        self.assertEqual(ev, "status")
        self.assertEqual(data["status"], "COMPLETE")
        conn.close()

    def test_orchestrator_writes_agent_start_end_markers_around_every_agent_call(self):
        o = self.run_task()
        ev = [json.loads(x) for x in (o.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        marks = [(e["event"], e["data"]["agent"], e["data"]["role"], e["cycle"]) for e in ev if e["event"] in ("agent_start", "agent_end")]
        expected = [("codex", "planner", 0), ("claude", "executor", 1), ("orchestrator", "verification", 1), ("codex", "reviewer", 1),
                    ("claude", "executor", 2), ("orchestrator", "verification", 2), ("codex", "reviewer", 2)]
        want = [x for a, r, c in expected for x in (("agent_start", a, r, c), ("agent_end", a, r, c))]
        self.assertEqual(marks, want)

    def test_reader_phase_rules(self):
        d = self.runs / "phase-task"
        d.mkdir()
        r = wv.RunReader(d, wv.Redactor())
        def ev(seq, event, **data):
            with open(d / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"seq": seq, "ts": "2026-10-12T01:00:00+00:00", "event": event, "cycle": 1, "data": data}) + "\n")
        ev(1, "agent_start", agent="codex", role="planner"); r.poll()
        self.assertEqual(r.phase["agent"], "codex")
        ev(2, "agent_end", agent="codex", role="planner"); r.poll()
        self.assertIsNone(r.phase)
        ev(3, "agent_start", agent="claude", role="executor"); ev(4, "task_end", status="STOPPED"); r.poll()
        self.assertIsNone(r.phase, "a finished task never shows a working agent")


class TestLaunchIntegration(WebBase):
    def setUp(self):
        super().setUp()
        self.cfgfile = self.base / "config.json"
        self.cfgfile.write_text(json.dumps({"runs_dir": str(self.runs), "verify_command": UNITTEST_CMD}), encoding="utf-8")
        self.scn = self.base / "s.json"
        self.scn.write_text(json.dumps(ms.rejection_scenario()), encoding="utf-8")
        self.out: list[str] = []

    def main(self, *args):
        return orch.main(["--config", str(self.cfgfile), *args], out=self.out.append)

    def url_in_output(self):
        m = re.search(r"LIVE VIEWER \(localhost only\): (http://127\.0\.0\.1:(\d+)/\?t=[\w-]+&task=[\w-]+)", "\n".join(self.out))
        return m and (m.group(1), int(m.group(2)))

    def test_run_with_web_starts_the_viewer_before_the_task_prints_the_url_and_shuts_down(self):
        with mock.patch("web_viewer.webbrowser.open", return_value=True) as opened:
            rc = self.main("--web", "run", "--project", str(self.ws), "--task", "t", "--scenario", str(self.scn))
        self.assertEqual(rc, 0)
        url, port = self.url_in_output()
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0], url, "the browser is opened automatically with the tokenised URL")
        self.assertLess([i for i, l in enumerate(self.out) if "LIVE VIEWER" in l][0], [i for i, l in enumerate(self.out) if "[plan]" in l][0])
        with self.assertRaises(OSError):  # server stopped when the task ended (hold-viewer defaults to 0)
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_no_open_prints_url_but_never_launches_a_browser(self):
        with mock.patch("web_viewer.webbrowser.open", side_effect=AssertionError("must not open")):
            rc = self.main("--web", "--no-open", "run", "--project", str(self.ws), "--task", "t", "--scenario", str(self.scn))
        self.assertEqual(rc, 0)
        self.assertTrue(self.url_in_output())

    def test_viewer_is_off_by_default_for_non_interactive_callers_and_with_no_web(self):
        with mock.patch("web_viewer.webbrowser.open", side_effect=AssertionError("must not open")):
            self.assertEqual(self.main("run", "--project", str(self.ws), "--task", "t", "--scenario", str(self.scn)), 0)
            ws2 = self.new_ws("ws2")
            self.assertEqual(self.main("--no-web", "run", "--project", str(ws2), "--task", "t", "--scenario", str(self.scn)), 0)
        self.assertIsNone(self.url_in_output())

    def test_hold_viewer_keeps_the_server_available_after_the_task_for_replay(self):
        holder = {}
        real_sleep = time.sleep
        def fake_hold(sec):
            url, port = self.url_in_output()
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c.request("GET", "/api/tasks?t=" + re.search(r"t=([\w-]+)", url).group(1))
            holder["tasks"] = json.loads(c.getresponse().read())["tasks"]
            c.close()
            holder["slept"] = sec
        with mock.patch("cli.time.sleep", side_effect=fake_hold):
            rc = self.main("--web", "--no-open", "--hold-viewer", "30", "run", "--project", str(self.ws), "--task", "t", "--scenario", str(self.scn))
        self.assertEqual((rc, holder["slept"], holder["tasks"][0]["status"], holder["tasks"][0]["messages"]), (0, 30, "COMPLETE", 11))

    def test_serve_command_for_saved_tasks_and_timeout(self):
        o = self.run_task()
        t0 = time.time()
        with mock.patch("web_viewer.webbrowser.open", side_effect=AssertionError("must not open")):
            rc = self.main("--no-open", "serve", "--task-id", o.task_id, "--timeout", "0.6")
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(time.time() - t0, 0.5)
        self.assertIn(o.task_id, self.url_in_output()[0])

    def test_paced_demo_is_slow_enough_to_watch(self):
        t0 = time.time()
        rc = mock_scenarios.run_demo(load_config(None, {"runs_dir": str(self.runs)}), self.out.append, None, delay=0.06)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(time.time() - t0, 0.25)   # 5 mock agent calls x 0.06s

    def test_viewer_config_is_validated_and_always_loopback(self):
        self.assertEqual(DEFAULT_CONFIG["viewer"], {"web": True, "auto_open_browser": True, "port": 0})
        for bad in ({"web": "yes", "auto_open_browser": True, "port": 0}, {"web": True, "auto_open_browser": True, "port": 70000},
                    {"web": True, "auto_open_browser": True, "port": 0, "host": "0.0.0.0"}, {"web": True}):
            with self.subTest(bad), self.assertRaises(ConfigError):
                load_config(None, {"viewer": bad})


class TestHebrewSummaries(WebBase):
    """Optional agent-written `user_summary_he` (v2.1): validated, display-only, backward compatible, MOCK clearly flagged."""

    def test_validators_accept_optional_summary_and_ignore_bad_ones_without_failing(self):
        import agents
        plan = {"plan": "p", "acceptance_criteria": ["tests/test_calc.py passes"], "instruction": "do it"}
        for extra, ignored in (({}, []), ({"user_summary_he": "תקציר"}, []), ({"user_summary_he": ""}, []),
                               ({"user_summary_he": 5}, ["user_summary_he"]), ({"user_summary_he": "א" * 1501}, ["user_summary_he"])):
            d, ign, perm = agents.validate_plan(dict(plan, **extra), 4000)
            self.assertEqual((ign, perm), (ignored, []))
            self.assertNotIn("user_summary_he", d, "the summary is display-only and never forwarded")
        rep, ign, _ = agents.validate_report({"summary": "s", "user_summary_he": "סיכום"}, 10)
        self.assertEqual(ign, []); self.assertNotIn("user_summary_he", rep)
        rv, ign, _ = agents.validate_review({"verdict": "COMPLETE", "findings": [], "user_summary_he": "אושר"}, 4000)
        self.assertEqual(ign, []); self.assertNotIn("user_summary_he", rv)

    def test_schemas_and_prompts_ask_for_the_summary_in_the_same_call(self):
        import claude_adapter as cla
        import codex_adapter as ca
        for sch in (ca.PLAN_SCHEMA, ca.REVIEW_SCHEMA):   # Codex strict mode: listed in required, "" allowed
            self.assertEqual(sch["properties"]["user_summary_he"], {"type": "string"})
            self.assertIn("user_summary_he", sch["required"])
        self.assertIn("user_summary_he", cla.REPORT_SCHEMA["properties"])
        self.assertNotIn("user_summary_he", cla.REPORT_SCHEMA["required"], "optional for Claude")
        for prompt in (ca.PLANNER_PROMPT, ca.REVIEWER_PROMPT, cla.FRAMING):
            self.assertIn("user_summary_he", prompt); self.assertIn("plain Hebrew", prompt)

    def test_mock_summary_run_is_flagged_mock_and_summaries_stay_out_of_the_reviewer_input(self):
        o, (planner, executor, reviewer) = self.make(mock_scenarios.he_summary_scenario())
        st = o.start(mock_scenarios.HE_SUMMARY_TASK)
        self.assertEqual(st["status"], "COMPLETE")
        self.assertEqual(set(st["agents"].values()), {"MockPlanner", "MockExecutor", "MockReviewer"})
        _, snap = self.jget(f"/api/snapshot?task={o.task_id}")
        self.assertTrue(snap["status"]["is_mock"])
        self.assertEqual(snap["status"]["short_title"], mock_scenarios.HE_SUMMARY_TASK)
        with_sum = [m["kind"] for m in snap["messages"] if "user_summary_he" in m["content"]]
        self.assertEqual(with_sum, ["plan", "report", "review", "report", "review"])
        self.assertIn("הבדיקות", json.loads(snap["messages"][-2]["content"])["user_summary_he"])  # stored as real UTF-8
        for call in reviewer.calls:
            self.assertNotIn("user_summary_he", call["executor_report_UNTRUSTED"])
        self.assertNotIn("untrusted_fields_ignored", self.event_names(o))
        _, lst = self.jget("/api/tasks")
        self.assertTrue(next(t for t in lst["tasks"] if t["id"] == o.task_id)["is_mock"])

    def test_only_positive_evidence_marks_a_run_mock(self):
        self.assertFalse(wv.RunReader.is_mock({}))
        self.assertFalse(wv.RunReader.is_mock({"agents": {"planner": "CodexPlanner", "executor": "MockExecutor", "reviewer": "CodexReviewer"}}))
        self.assertFalse(wv.RunReader.is_mock({"agents": {"planner": "CodexPlanner", "executor": "ClaudeExecutor", "reviewer": "CodexReviewer"}}))
        self.assertTrue(wv.RunReader.is_mock({"workspace": r"C:\Temp\orch-demo-abc\workspace"}))  # older demo runs
        d = self.runs / "harness"
        conv.Transcript(d / "transcript.jsonl").add("user", "codex", "task", "Implement this\nsecond line", 0)
        stat = self.jget("/api/snapshot?task=harness")[1]["status"]
        self.assertEqual((stat["is_mock"], stat["short_title"]), (False, "Implement this"))
        self.assertEqual(wv.short_title("x" * 200)[-1], "…")

    def test_page_has_collapsed_code_controls_attribution_and_one_outcome(self):
        src = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for needed in ("הצג קוד", "הצג בדיקות", "הודעה מקורית", "תיאור המערכת · לא נכתב ע״י", "תקציר בעברית שכתב",
                       "MOCK: נכתב מראש לדוגמה, לא ע״י מודל", "תוצאת המשימה", "יישום אומת בפועל", "קוד נכתב לפרויקט",
                       "על סמך קריאה בלבד", "הורצו ונכשלו", "הסתר פאנל"):
            self.assertIn(needed, src)
        self.assertNotIn("outcomeCard", src, "the outcome is rendered once, in the conversation")
        self.assertEqual(src.count("resultCard("), 2, "defined once, used once")


if __name__ == "__main__":
    unittest.main()
