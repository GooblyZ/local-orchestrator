"""`start` - the everyday way to run a REAL Codex <-> Claude task (start.cmd calls this).

NATIVE profile (default): Claude Code works as usual - reads, edits and writes files in the task folder and runs the
project's tests - limited by Claude Code's own permission system (config `claude.allowed_tools`, `--permission-mode
dontAsk`: anything not pre-approved is DENIED, never silently granted). When Claude needs something else, the run pauses
and the owner approves or denies that exact rule in this window, for this task only; the same Claude session continues.
The orchestrator re-runs the tests itself and gives Codex the real changed files and test output.
RESTRICTED profile (`--restricted --allow FILE`): Claude has no tools; the orchestrator writes allowlisted files and checks
them statically; nothing generated is executed.

Steps: task + folder -> non-inference preflight of both CLIs -> summary -> owner types "כן" (this accepts local execution
in the native profile; nothing persistent is created; refused without an interactive terminal) -> browser viewer opens and
must connect -> run -> honest outcome. Ctrl+C pauses (resume later with `orchestrator.py resume`).
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from config import HERE, OrchestratorError, load_config
from run_policy import budget_limits

STATUS_HE = {"COMPLETE": "הושלם — Codex אישר, והבדיקות שהמערכת הריצה עברו",
             "NEEDS_REVISION": "לא הושלם — נדרש תיקון נוסף (נגמרו הסבבים או תקציב הקריאות)",
             "FAILED": "נכשל", "BLOCKED": "חסום — בעיה חיצונית (התחברות, מכסה או הגדרות)",
             "HUMAN_REQUIRED": "ממתין להחלטה שלך", "PAUSED": "מושהה — אפשר להמשיך עם resume", "STOPPED": "נעצר לבקשתך"}
CONFIRM = {"כן", "yes", "y"}


def _ask(inp, out, prompt, default=""):
    out(prompt + (f" [{default}]" if default else ""))
    v = (inp("> ") or "").strip()
    return v or default


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")[:30]
    return s or "task"


def running_app(info_path: Path):
    """URL of an app server that is already running (same user), else None. Prevents duplicate servers."""
    import json
    import urllib.request
    from safety import pid_alive
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if not pid_alive(int(info["pid"])):
            return None
        u = info["url"]
        with urllib.request.urlopen(u.replace("/?", "/api/app/state?", 1), timeout=3) as r:  # noqa: S310 - 127.0.0.1 only
            return u if r.status == 200 and u.startswith("http://127.0.0.1:") else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def run_app(a, out=print, service=None, serve_forever=True) -> int:
    """The browser app (start.cmd): one local server; tasks are entered, approved and watched in the browser."""
    import os
    import web_viewer
    from app_service import AppService
    from orchestrator import resolve_runs_dir
    from store import atomic_write_json
    cfg = load_config(a.config if Path(a.config).exists() else None)
    runs = resolve_runs_dir(cfg)
    runs.mkdir(parents=True, exist_ok=True)
    info = runs / "_app.json"
    url = running_app(info)
    if url:
        out("המערכת כבר פועלת — פותח את החלון הקיים בדפדפן.")
        if not a.no_open:
            web_viewer.open_browser(url)
        return 0
    try:
        srv = web_viewer.ViewerServer(runs, a.port or 0, app=service or AppService(a.config)).start()
    except OSError as e:
        out(f"לא ניתן להפעיל את השרת המקומי: {e}")
        return 2
    url = srv.url()
    atomic_write_json(info, {"pid": os.getpid(), "port": srv.port, "url": url})
    out("=== Codex + Claude ===")
    out("המערכת פועלת בדפדפן. אל תסגור את החלון הזה בזמן שמשימה רצה (אפשר למזער אותו).")
    out(f"כתובת (במחשב הזה בלבד): {url}")
    if not a.no_open and not web_viewer.open_browser(url):
        out("לא ניתן היה לפתוח דפדפן אוטומטית — העתק את הכתובת למעלה.")
    if not serve_forever:
        return srv
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        out("עוצר את השרת...")
    finally:
        srv.stop()
        try:
            if json_pid(info) == os.getpid():
                info.unlink()
        except OSError:
            pass
    return 0


def json_pid(p: Path):
    import json
    try:
        return int(json.loads(p.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _permission_decision(st, pending, orc, cfg, runs, ws, agents, out, inp):
    from orchestrator import Orchestrator
    from taskctl import resolve_tool_permissions
    out("")
    out("Claude ביקש לבצע פעולה שאינה מאושרת מראש (הפעולה נחסמה, שום דבר לא אושר אוטומטית):")
    for d in pending:
        out(f"  • {d.get('tool')}: {d.get('input') or ''}" + (f"  → כלל מוצע: {d['suggested_rule']}" if d.get("suggested_rule") else "  (לא ניתן לאשר פעולה כזו)"))
    rules = [d["suggested_rule"] for d in pending if d.get("suggested_rule")]
    approve = False
    if rules:
        approve = _ask(inp, out, 'לאשר למשימה הזו בלבד ולהמשיך? הקלד "כן" לאישור, או Enter לסירוב').strip().lower() in CONFIRM
    else:
        out("אין כאן כלל שמותר לאשר. Claude יתבקש להשלים את המשימה בלי הפעולה הזו.")
    resolve_tool_permissions(runs, orc.task_id, rules, approve)
    out("אושר. ממשיך באותה שיחה עם Claude..." if approve else "לא אושר. ממשיך בלי ההרשאה הזו...")
    return Orchestrator(cfg, ws, runs, *agents, out=out, task_id=orc.task_id, local_execution=True).resume()


def run_start(a, out=print, inp=input, tty=None, codex_runner=None, codex_base_dir=None, claude_runner=None, claude_which=None) -> int:
    from claude_adapter import ClaudeCli, ClaudeExecutor
    from codex_adapter import CodexCli, CodexPlanner, CodexReviewer
    from agents import AdapterError
    from orchestrator import EXIT, Orchestrator, resolve_runs_dir
    from proposals import make_file_spec
    from safety import Policy
    import web_viewer

    from store import ACTIVE, read_json
    from taskctl import resolve_task
    interactive = sys.stdin.isatty() if tty is None else tty
    cfg = load_config(a.config if Path(a.config).exists() else None, {"max_cycles": a.max_cycles})
    runs = resolve_runs_dir(cfg)
    out("=== מתזמר AI מקומי: Codex מנהל, Claude מתכנת ===")
    resuming, prior = None, {}
    if a.resume is not None:  # continue a paused / waiting task (e.g. after Ctrl+C), with a fresh confirmation
        try:
            resuming = resolve_task(runs, a.resume or None)
        except OrchestratorError as e:
            out(f"לא נמצאה משימה להמשך: {e}")
            return 2
        prior = read_json(runs / resuming / "state.json")
        if prior.get("status") not in ACTIVE:
            out(f"המשימה {resuming} במצב {prior.get('status')} ואי אפשר להמשיך אותה.")
            return 2
        cfg["max_cycles"] = min(50, prior.get("cycle", 0) + a.max_cycles)
    cfg["max_codex_calls"], cfg["max_claude_calls"] = 1 + cfg["max_cycles"], cfg["max_cycles"]
    task = prior.get("task") or a.task or (_ask(inp, out, "מה המשימה? (תיאור בשורה אחת)") if interactive else "")
    if not task.strip():
        out("לא הוזנה משימה.")
        return 2
    restricted = bool(prior.get("file_spec")) if resuming else bool(a.restricted)
    allow = [x.strip() for x in (a.allow or [])]
    if restricted and not allow and interactive and not resuming:
        allow = [x.strip() for x in _ask(inp, out, "אילו קבצים מותר ל-Claude ליצור? מופרדים בפסיקים (למשל: calc.py, tests/test_calc.py)").split(",")]
    allow = [x for x in allow if x]
    if restricted and not allow and not resuming:
        out("במצב המוגבל חובה לציין לפחות קובץ אחד שמותר ליצור.")
        return 2
    if resuming:
        ws = Path(prior["workspace"])
    elif a.project:
        ws = Path(a.project).resolve()
    else:
        ws = HERE / "workspaces" / (time.strftime("%Y%m%d-%H%M%S") + "-" + _slug(task))  # created only after "כן"
    spec = prior.get("file_spec") if resuming else None
    if restricted and not resuming:
        try:
            spec = make_file_spec(allow, a.allow_overwrite or [], a.require_def or [], Policy(ws, cfg))
        except ValueError as e:
            out(f"רשימת הקבצים אינה תקינה: {e}")
            return 2
    out("בודק את Codex ואת Claude (בלי לפנות למודל)...")
    try:
        codex = CodexCli(cfg, runner=codex_runner, **({"base_dir": codex_base_dir} if codex_base_dir else {}))
        codex.preflight()
        claude = ClaudeCli(cfg, runner=claude_runner, **({"which": claude_which} if claude_which else {}))
        claude.preflight(str(ws if ws.exists() else HERE))
    except AdapterError as e:
        out(f"BLOCKED — הבדיקה המקדימה נכשלה ולכן לא הופעל אף מודל: {e.message}")
        return EXIT["BLOCKED"]
    lim = budget_limits(cfg)
    out("")
    out(f"משימה: {task}" + (f"  (המשך של {resuming}, מסבב {prior.get('cycle')})" if resuming else ""))
    out(f"תיקיית עבודה: {ws}")
    out(f"עד {a.max_cycles} סבבים · עד {lim['codex']} קריאות ל-Codex ועד {lim['claude']} ל-Claude · דרך המנויים (ללא API בתשלום)")
    if restricted:
        out(f"מצב מוגבל · קבצים מותרים: {', '.join(spec['create'] + spec['overwrite'])}")
        out("Claude יפעל בלי כלים: רק מציע קבצים. המערכת כותבת רק קבצים מותרים ובודקת אותם בלי להריץ את הקוד.")
    else:
        out("מצב רגיל של Claude Code: קורא, עורך וכותב קבצים בתיקיית העבודה ומריץ את הבדיקות של הפרויקט.")
        out("מותר בלי לשאול: " + ", ".join(cfg["claude"]["allowed_tools"]) + ". כל דבר אחר ייעצר ויחכה לאישור שלך.")
        out("שים לב: הקוד ירוץ במחשב הזה בהרשאות Windows שלך — אותו סיכון כמו שימוש רגיל ב-Claude Code.")
        out("המערכת תריץ בעצמה את הבדיקות ותציג ל-Codex את הקבצים שהשתנו בפועל ואת תוצאות הבדיקות.")
    if not interactive:
        out("נדרש אישור בהקלדה בחלון טרמינל אינטראקטיבי — הריצה לא התחילה.")
        return EXIT["HUMAN_REQUIRED"]
    if _ask(inp, out, 'להתחיל? הקלד "כן" לאישור' + ("" if restricted else " (כולל הרצה מקומית)")).strip().lower() not in CONFIRM:
        out("בוטל. לא הופעל אף מודל.")
        return EXIT["STOPPED"]
    ws.mkdir(parents=True, exist_ok=True)
    agents = (CodexPlanner(codex), ClaudeExecutor(claude), CodexReviewer(codex))
    orc = Orchestrator(cfg, ws, runs, *agents, out=out, file_spec=spec, local_execution=not restricted, task_id=resuming)
    srv = None
    if not a.no_web:
        orc.run_dir.mkdir(parents=True, exist_ok=True)  # the page must find the (still empty) run, or it waits forever
        srv = web_viewer.start_for_task(runs, orc.task_id, a.port or cfg["viewer"]["port"], not a.no_open, out)
        out("ממתין שהדפדפן יתחבר לחלון השיחה...")
        if not srv.wait_for_viewer(a.wait_viewer):
            out("הדפדפן לא התחבר. פתח את הכתובת למעלה והקש Enter להמשך (או Ctrl+C לביטול).")
            try:
                inp("> ")
            except KeyboardInterrupt:
                srv.stop()
                out("בוטל. לא הופעל אף מודל.")
                return EXIT["STOPPED"]
    try:
        st = orc.resume() if resuming else orc.start(task)
        for _ in range(5):  # Claude asked for something not pre-approved: the owner decides here, for this task only
            pending = st.get("pending_permissions") if st["status"] == "HUMAN_REQUIRED" else None
            if not pending:
                break
            st = _permission_decision(st, pending, orc, cfg, runs, ws, agents, out, inp)
    except OrchestratorError as e:
        out(f"לא ניתן להתחיל: {e}")
        if srv:
            srv.stop()
        return 2
    calls = st.get("calls") or {}
    out("")
    status_text = STATUS_HE.get(st["status"], st["status"])
    if restricted and st["status"] == "COMPLETE":
        status_text = "הושלם — Codex אישר והבדיקה הסטטית עברה (במצב המוגבל הקוד לא הורץ)"
    out(f"תוצאה: {status_text}")
    out(f"סיבה (כפי שנרשמה): {st.get('stop_reason')}")
    if restricted:
        written = sorted((st.get("file_spec") or {}).get("applied", {}))
        out(f"קבצים שנכתבו ונבדקו: {', '.join(written) if written else 'אין'} · בתיקייה: {ws}")
    else:
        last = (st.get("history") or [{}])[-1]
        out(f"הבדיקות שהמערכת הריצה בסבב האחרון: {last.get('verification')} (מספר בדיקות: {last.get('tests_ran')}) · התיקייה: {ws}")
    out(f"קריאות למודלים: Codex {calls.get('codex', 0)}, Claude {calls.get('claude', 0)}")
    if srv:
        out("חלון השיחה בדפדפן נשאר פתוח לעיון. הקש Enter לסגירה.")
        try:
            inp("> ")
        except (KeyboardInterrupt, EOFError):
            pass
        srv.stop()
    return EXIT.get(st["status"], 1)
