"""Local-only browser viewer for the Codex <-> Claude conversation (standard library, no dependencies).

It READS what the orchestrator already writes (runs/<task>/transcript.jsonl, events.jsonl, state.json), so it works
live from a second process and for replaying finished runs, and shows the stored, already-redacted message text unchanged
(a second redaction pass is applied as defense in depth). `serve` is read-only (GET only). In APP mode (`orchestrator.py
app`) a few POST actions exist (app_service.AppService: prepare/start a task, permission decisions, pause/stop/resume,
open folder); each POST needs the per-launch token in a header (never the URL), a CSRF value that exists only inside the
served page, a same-origin Origin header and a JSON body - a GET or a link can never change anything.

Security: binds 127.0.0.1 only; every request needs a random per-launch token and a localhost Host header (blocks
other web pages / DNS rebinding); strict CSP (no external resources, nonce'd inline script); no directory listing;
task ids are validated against existing run directories; the page builds the DOM with textContent only (agent text
can never be interpreted as HTML).

Streaming honesty: messages are shown when the orchestrator records them (complete), never as invented partials.
"Codex is working..." comes from real agent_start/agent_end events plus real CLI progress events.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from conversation import read_transcript
from safety import Redactor, pid_alive
from store import _lock_file

HERE = Path(__file__).resolve().parent
TASK_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
INDEX = HERE / "web" / "index.html"
ATTACHMENTS = ("claude_envelope.json", "live_claude_envelope.json")  # raw CLI envelopes saved by test harnesses
MAX_POST_BYTES = 2_000_000  # one JSON request (a 200,000-character Hebrew task is ~400 KB of UTF-8)
ACTIVE_WINDOW_SEC = 180  # harness runs have no lock: "active" only if the transcript changed recently


def _read_lines(path: Path, offset: int):
    """Complete JSONL lines after `offset` -> (records, new_offset). A half-written last line is left for the next read."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    out, used = [], 0
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break
        used += len(raw)
        try:
            rec = json.loads(raw.decode("utf-8"))
            if isinstance(rec, dict):
                out.append(rec)
        except (ValueError, UnicodeDecodeError):
            pass
    return out, offset + used


class RunReader:
    """Incremental, tolerant reader for one run directory (files may not exist yet or be half-written)."""

    def __init__(self, run_dir: Path, redactor: Redactor):
        self.dir, self.red = Path(run_dir), redactor
        self.t_off = self.e_off = 0
        self.messages: list[dict] = []
        self.activity: list[dict] = []
        self.events: list[dict] = []
        self.phase: dict | None = None

    def _clean_msg(self, m: dict) -> dict:
        m = dict(m)
        m["content"] = self.red.text(str(m.get("content", "")))
        m["meta"] = self.red.obj(m.get("meta") or {})
        return m

    def poll(self):
        new_msgs, self.t_off, _ = read_transcript(self.dir / "transcript.jsonl", self.t_off)
        new_msgs = [self._clean_msg(m) for m in new_msgs]
        events, self.e_off = _read_lines(self.dir / "events.jsonl", self.e_off)
        new_act, new_ev = [], []
        for e in events:
            ev, data = e.get("event"), e.get("data") or {}
            if ev == "activity":
                new_act.append({"eseq": e.get("seq"), "ts": data.get("ts") or e.get("ts"), "source": str(data.get("source", "?")),
                                "text": self.red.text(str(data.get("text", "")))})
            else:  # every other orchestrator event is kept for the technical view (already clipped when written)
                new_ev.append({"eseq": e.get("seq"), "ts": e.get("ts"), "event": str(ev), "cycle": e.get("cycle"),
                               "data": self.red.obj(data)})
            if ev == "agent_start":
                self.phase = {"agent": data.get("agent"), "role": data.get("role"), "since": e.get("ts"), "cycle": e.get("cycle")}
            elif ev in ("agent_end", "task_end", "paused", "human_required", "task_resume"):
                self.phase = None
        self.messages += new_msgs
        self.activity += new_act
        self.events += new_ev
        return new_msgs, new_act, new_ev

    def _lock_alive(self, st: dict) -> bool:
        """True only if THIS task currently holds its workspace lock from a running process."""
        try:
            holder = json.loads(_lock_file(self.dir.parent, st.get("workspace", "")).read_text(encoding="utf-8"))
            return holder.get("task_id") == self.dir.name and pid_alive(int(holder.get("pid", 0)))
        except (OSError, ValueError, TypeError):
            return False

    def run_state(self, st: dict) -> str:
        """active | saved | interrupted | waiting | awaiting_owner - never 'active' just because the viewer is connected."""
        if st.get("status") == "AWAITING_OWNER":  # a prepared one-shot test that has NOT started: no agent may run yet
            return "awaiting_owner"
        if st.get("status"):
            if st["status"] == "RUNNING":
                return "active" if self._lock_alive(st) else "interrupted"
            return "saved"
        if not self.messages:
            return "waiting"
        if self.messages[-1].get("kind") == "final":
            return "saved"
        try:
            age = time.time() - (self.dir / "transcript.jsonl").stat().st_mtime
        except OSError:
            age = 1e9
        return "active" if age < ACTIVE_WINDOW_SEC else "interrupted"

    @staticmethod
    def is_mock(st: dict) -> bool:
        """True only with positive evidence that NO model was called: every recorded adapter is a Mock* class, or the
        run used the `demo` command's temporary workspace (both demo variants call no model)."""
        agents = st.get("agents")
        if isinstance(agents, dict) and agents:
            return all(str(v).startswith("Mock") for v in agents.values())
        return Path(str(st.get("workspace") or "")).parent.name.startswith("orch-demo-")

    def attachments(self) -> list[dict]:
        out = []
        for name in ATTACHMENTS:
            p = self.dir / name
            try:
                if p.is_file() and p.stat().st_size < 1_000_000:
                    out.append({"name": name, "data": self.red.obj(json.loads(p.read_text(encoding="utf-8")))})
            except (OSError, ValueError):
                continue
        return out

    def _internal_error(self):
        """The orchestrator's fallback record when even state.json could not be written (see Orchestrator._internal_error)."""
        try:
            return self.red.obj(json.loads((self.dir / "internal_error.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def status(self) -> dict:
        st = {}
        try:
            st = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        title = st.get("task") or next((m["content"] for m in self.messages if m.get("kind") == "task"), "")
        status = st.get("status")
        source = "state"
        if not status:
            source = "derived from transcript"
            last = self.messages[-1] if self.messages else None
            if last and last.get("kind") == "final":
                status = last["content"].split(":", 1)[0].strip() or "ENDED"
            else:
                status = "RUNNING" if self.messages else "WAITING"
        return {"task_id": self.dir.name, "title": self.red.text(str(title))[:400], "status": status,
                "cycle": st.get("cycle", max([m.get("cycle") or 0 for m in self.messages] or [0])),
                "max_cycles": st.get("max_cycles"), "stop_reason": self.red.text(str(st.get("stop_reason") or "")),
                "completion_kind": st.get("completion_kind"), "phase": self.phase, "messages": len(self.messages),
                "status_source": source, "run_state": self.run_state(st), "created_at": st.get("created_at"),
                "updated_at": st.get("updated_at"), "is_mock": self.is_mock(st), "workspace": st.get("workspace"),
                "project": st.get("project"), "handed_off_to": st.get("handed_off_to") or [],
                "autonomy": _autonomy_view(st), "decisions": self.red.obj((st.get("decisions") or [])[-40:]),
                "metrics": st.get("metrics") or {}, "pending_decision": self.red.obj(st.get("pending_decision")),
                "blocker": self.red.obj(st.get("blocker")),
                "nonblocking_suggestions": self.red.obj(st.get("nonblocking_suggestions") or []),
                "pending_permissions": self.red.obj(_rescreen(st.get("pending_permissions") or [], st.get("workspace"))),
                "pending_question": self.red.text(str(st.get("pending_question") or ""))[:2000],
                "continuations": self.red.obj(st.get("continuations") or []), "continuation_pending": st.get("continuation_pending"),
                "review_pending": bool(st.get("review_pending")),
                "needs_owner": needs_owner(st, self.run_state(st)) if st else False,
                "approved_tools": st.get("approved_tools") or [], "local_execution": bool(st.get("local_execution")),
                "agent_settings": st.get("agent_settings"), "mode": st.get("mode") or "fixed", "kind": st.get("kind") or "code",
                "budget": st.get("budget"), "elapsed_sec": st.get("elapsed_sec"), "calls": st.get("calls"),
                "acceptance_criteria": self.red.obj(st.get("acceptance_criteria") or []),
                "criteria_status": st.get("criteria_status"), "strategy": self.red.text(str(st.get("instruction") or ""))[:1500],
                "short_title": short_title(self.red.text(str(title))),
                "internal_error": self._internal_error(),
                "status_summary": self.red.obj(st.get("status_summary")),
                "status_summary_history": self.red.obj((st.get("status_summary_history") or [])[-20:]), "summary_api": True}


_SCREEN_CACHE: dict = {}


def _rescreen(pending: list, workspace) -> list:
    """A pending script card shows what the CURRENT analyzer finds in that exact file version (cached per rule), so the owner
    is never shown stale findings from an older screening. Read-only."""
    out = []
    for d in pending:
        rule = str(d.get("suggested_rule") or "")
        if d.get("kind") == "script" and rule.startswith("script:") and workspace:
            key = (workspace, rule)
            if key not in _SCREEN_CACHE:
                try:
                    import capgate
                    g = capgate.Gate({"root": workspace, "audit": "", "protected": []})
                    _SCREEN_CACHE[key] = g.screen(g.inside(rule[len("script:"):].rsplit("@", 1)[0]))
                except (ValueError, OSError):
                    _SCREEN_CACHE[key] = None
            if _SCREEN_CACHE[key] is not None:
                d = dict(d, findings=_SCREEN_CACHE[key])
        out.append(d)
    return out


def _autonomy_view(st: dict) -> dict:
    import autonomy
    return autonomy.policy(st) if st else {}


def short_title(text: str, limit: int = 90) -> str:
    """A readable one-line title from the task text (its first non-empty line, clipped). Never model text."""
    line = next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")
    line = re.sub(r"^(#{1,6}|[-*>]+)\s+", "", line).replace("**", "").strip() or line  # Markdown heading/emphasis marks
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def needs_owner(st: dict, run_state: str) -> bool:
    """The task is waiting for the OWNER: a question, a permission, a budget, a pause, an interruption or a recoverable failure."""
    s = st.get("status")
    if st.get("handed_off_to"):  # its work continues in another task; this one is kept for reference only
        return False
    if s in ("HUMAN_REQUIRED", "PAUSED", "AWAITING_OWNER") or run_state == "interrupted":
        return True
    return (s == "FAILED" and st.get("kind") == "research" and bool(st.get("plan"))
            and str(st.get("stop_reason") or "").startswith(("malformed_output", "reviewer_error")))


def list_tasks(runs_dir: Path, redactor: Redactor) -> list[dict]:
    out = []
    if Path(runs_dir).is_dir():
        for d in Path(runs_dir).iterdir():
            if d.is_dir() and TASK_RE.match(d.name) and (d / "transcript.jsonl").exists():
                r = RunReader(d, redactor)
                r.poll()
                s = r.status()
                out.append({"id": d.name, "title": s["title"][:120], "short_title": s["short_title"], "status": s["status"],
                            "messages": s["messages"], "is_mock": s["is_mock"], "run_state": s["run_state"],
                            "kind": s["kind"], "stop_reason": s["stop_reason"][:300], "needs_owner": s["needs_owner"],
                            "project_mode": (s["project"] or {}).get("mode"), "handed_off": bool(s["handed_off_to"]),
                            "mtime": (d / "transcript.jsonl").stat().st_mtime})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # a browser closing a connection early is not an error to print
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


class ViewerServer:
    def __init__(self, runs_dir, port: int = 0, token: str | None = None, app=None):
        self.runs_dir = Path(runs_dir)
        self.token = token or secrets.token_urlsafe(18)
        self.app = app  # app_service.AppService in app mode; None = read-only viewer
        self.csrf = secrets.token_urlsafe(18)
        self.redactor = Redactor()
        self.stop_flag = threading.Event()
        self.streams = 0  # browsers currently connected to a live stream (lets a launcher wait until the owner watches)
        self._count_lock = threading.Lock()
        self.httpd = _QuietServer(("127.0.0.1", port), _make_handler(self))  # localhost only, never 0.0.0.0
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_flag.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def wait_for_viewer(self, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.streams > 0:
                return True
            time.sleep(0.2)
        return self.streams > 0

    def url(self, task_id: str | None = None) -> str:
        q = f"?t={self.token}" + (f"&task={task_id}" if task_id else "")
        return f"http://127.0.0.1:{self.port}/{q}"

    def run_dir(self, task_id: str) -> Path | None:
        if not task_id or not TASK_RE.match(task_id):
            return None
        d = self.runs_dir / task_id
        return d if d.is_dir() and d.resolve().parent == self.runs_dir.resolve() else None


def _make_handler(srv: ViewerServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OrchestratorViewer"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # keep the terminal clean
            pass

        # ---------------------------------------------------------------- helpers
        def _send(self, code, body: bytes, ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "json" in ctype else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _guard(self):
            """Returns parsed query or None after having answered with an error."""
            host = (self.headers.get("Host") or "").lower()
            if host not in (f"127.0.0.1:{srv.port}", f"localhost:{srv.port}"):
                self._json({"error": "bad host"}, 403)
                return None
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not secrets.compare_digest(q.get("t", ""), srv.token):
                self._json({"error": "forbidden"}, 403)
                return None
            return u.path, q

        # ---------------------------------------------------------------- routes
        def do_GET(self):
            g = self._guard()
            if not g:
                return
            path, q = g
            if path == "/":
                nonce = secrets.token_urlsafe(12)
                html = (INDEX.read_text(encoding="utf-8").replace("__NONCE__", nonce).replace("__TOKEN__", srv.token)
                        .replace("__CSRF__", srv.csrf if srv.app else "").replace("__APP__", "1" if srv.app else "0"))
                csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
                       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                return self._send(200, html.encode("utf-8"), "text/html", {"Content-Security-Policy": csp})
            if path == "/api/tasks":
                return self._json({"tasks": list_tasks(srv.runs_dir, srv.redactor)})
            if path.startswith("/api/app/"):
                if not srv.app:
                    return self._json({"error": "not found"}, 404)
                from app_service import AppError
                try:
                    if path == "/api/app/state":
                        return self._json(srv.app.state())
                    if path == "/api/app/options":
                        return self._json(srv.app.options())
                    if path == "/api/app/files":
                        return self._json(srv.app.files(q.get("task", "")))
                    if path == "/api/app/file":
                        return self._json(srv.app.read_file(q.get("task", ""), q.get("path", "")))
                except AppError as e:
                    return self._json({"error": e.message}, e.status)
                return self._json({"error": "not found"}, 404)
            d = srv.run_dir(q.get("task", ""))
            if path in ("/api/snapshot", "/stream") and d is None:
                return self._json({"error": "unknown task"}, 404)
            if path == "/api/snapshot":
                r = RunReader(d, srv.redactor)
                r.poll()
                return self._json({"messages": r.messages, "activity": r.activity, "events": r.events, "status": r.status(),
                                   "attachments": r.attachments(), "server_time": time.time()})
            if path == "/stream":
                return self._stream(d, int(q.get("after", 0) or 0), int(q.get("eafter", 0) or 0))
            self._json({"error": "not found"}, 404)

        def _stream(self, run_dir: Path, after: int, eafter: int):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            reader, last_status, last_beat = RunReader(run_dir, srv.redactor), None, time.time()

            def emit(event, obj):
                self.wfile.write(f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            with srv._count_lock:
                srv.streams += 1
            try:
                emit("hello", {"server_time": time.time()})
                while not srv.stop_flag.is_set():
                    msgs, acts, evs = reader.poll()
                    for m in msgs:
                        if m["seq"] > after:
                            emit("message", m)
                    for a in acts:
                        if (a.get("eseq") or 0) > eafter:
                            emit("activity", a)
                    for e in evs:
                        if (e.get("eseq") or 0) > eafter:
                            emit("sysevent", e)
                    st = reader.status()
                    if st != last_status:
                        emit("status", st)
                        last_status = st
                    if time.time() - last_beat > 10:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_beat = time.time()
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                with srv._count_lock:
                    srv.streams -= 1
            self.close_connection = True

        def _no(self):
            self._json({"error": "read-only viewer: only GET is supported"}, 405)

        def do_POST(self):
            if not srv.app:
                return self._no()
            host = (self.headers.get("Host") or "").lower()
            origins = (f"http://127.0.0.1:{srv.port}", f"http://localhost:{srv.port}")
            if host not in (f"127.0.0.1:{srv.port}", f"localhost:{srv.port}") or self.headers.get("Origin") not in origins:
                return self._json({"error": "bad origin"}, 403)
            if not (secrets.compare_digest(self.headers.get("X-Orch-Token", ""), srv.token)
                    and secrets.compare_digest(self.headers.get("X-Orch-CSRF", ""), srv.csrf)):
                return self._json({"error": "forbidden"}, 403)
            u = urlparse(self.path)
            if u.query:
                return self._json({"error": "parameters belong in the JSON body"}, 400)
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json({"error": "JSON required"}, 415)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= MAX_POST_BYTES:
                return self._json({"error": f"request_too_large: {n:,} bytes; the limit is {MAX_POST_BYTES:,}"}, 413)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "invalid JSON"}, 400)
            if not isinstance(body, dict):
                return self._json({"error": "JSON object required"}, 400)
            from app_service import AppError
            routes = {"/api/app/prepare": srv.app.prepare, "/api/app/start": srv.app.start, "/api/app/permission": srv.app.permission,
                      "/api/app/control": srv.app.control, "/api/app/open-folder": srv.app.open_folder, "/api/app/budget": srv.app.budget,
                      "/api/app/summarize": srv.app.summarize, "/api/app/continue": srv.app.continue_task,
                      "/api/app/autonomy": srv.app.set_autonomy}
            fn = routes.get(u.path)
            if not fn:
                return self._json({"error": "not found"}, 404)
            try:
                return self._json(fn(body))
            except AppError as e:
                return self._json({"error": e.message}, e.status)
            except Exception as e:  # never leave the page without an answer
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        do_PUT = do_DELETE = do_PATCH = _no

    return Handler


def latest_task_id(runs_dir: Path) -> str | None:
    t = list_tasks(runs_dir, Redactor())
    return t[0]["id"] if t else None


def open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def start_for_task(runs_dir, task_id: str | None, port: int = 0, open_in_browser: bool = True, out=print) -> ViewerServer:
    """Start the viewer, print its URL (always) and optionally open the system browser."""
    srv = ViewerServer(runs_dir, port).start()
    url = srv.url(task_id)
    out(f"LIVE VIEWER (localhost only): {url}")
    if open_in_browser and not open_browser(url):
        out("(could not open a browser automatically; open the URL above)")
    return srv
