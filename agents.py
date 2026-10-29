"""Agent adapter interface, strict output validation, MOCK adapters, live-mode refusal.

Adapters return RAW TEXT (what a CLI would print on stdout) or raise AdapterError.
The orchestrator parses and validates that text itself; nothing an agent says is
trusted or executed. A future real adapter only has to implement the same three
methods (plan / execute / review) and map CLI failures to AdapterError kinds
(see safety.classify_cli_error).
"""
from __future__ import annotations

import json
import re

from safety import GuardedFS

VERDICTS = ("COMPLETE", "CONTINUE", "HUMAN_REQUIRED", "FAILED")
_SESSION_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")
_PERMISSIONISH = re.compile(r"perm|allow|command|grant|sandbox|approv|polic|path|root|tool|exec|budget|limit|quota|spend|cycle", re.I)


class AdapterError(Exception):
    """kind: auth | quota | malformed | timeout | process | cancelled"""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}")
        self.kind, self.message = kind, message


class LiveNotEnabled(Exception):
    pass


# --------------------------------------------------------------------------- validation
def parse_json_object(raw) -> dict:
    if not isinstance(raw, str):
        raise AdapterError("malformed", "agent output is not text")
    try:
        obj = json.loads(raw.strip())
    except ValueError as e:
        raise AdapterError("malformed", f"invalid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise AdapterError("malformed", "JSON output is not an object")
    return obj


def _str(d, key, maxlen, required=False):
    v = d.get(key, "")
    if not isinstance(v, str):
        raise AdapterError("malformed", f"field '{key}' invalid: it must be a string")
    if len(v) > maxlen:
        raise AdapterError("malformed", f"field '{key}' invalid: {len(v):,} characters; the maximum is {maxlen:,}")
    if required and not v.strip():
        raise AdapterError("malformed", f"field '{key}' invalid: it is empty")
    return v


def _strlist(d, key, max_items, maxlen):
    v = d.get(key, [])
    if not isinstance(v, list):
        raise AdapterError("malformed", f"field '{key}' invalid: it must be a list of strings")
    if len(v) > max_items:
        raise AdapterError("malformed", f"field '{key}' invalid: {len(v)} items; the maximum is {max_items}")
    for i, x in enumerate(v):
        if not isinstance(x, str):
            raise AdapterError("malformed", f"field '{key}' invalid: item {i + 1} is not a string")
        if len(x) > maxlen:
            raise AdapterError("malformed", f"field '{key}' invalid: item {i + 1} has {len(x):,} characters; the maximum is {maxlen:,}")
    return v


# The implementation agent's report. A detailed research cycle legitimately needs a long summary (a 4,062-character summary
# was once rejected by an unstated 4,000 limit); these limits are stated in Claude's prompt (claude_adapter) and enforced here.
REPORT_LIMITS = {"summary": 12_000, "changed_files": (500, 300), "commands_run": (200, 500), "remaining_issues": (100, 1000)}
REPORT_LIMITS_TEXT = ("Limits: summary at most 12,000 characters (details belong in the files); changed_files at most 500 paths; "
                      "commands_run at most 200 items of 500 characters; remaining_issues at most 100 items of 1,000 characters.")


def _split(d, known):
    ignored = sorted(set(d) - set(known))
    if "user_summary_he" in d and not valid_summary_he(d["user_summary_he"]):
        ignored.append("user_summary_he")  # optional display-only field: a bad one is ignored, never fatal
    if "status_summary_he" in d and valid_status_summary(d["status_summary_he"]) is None:
        ignored.append("status_summary_he")
    return ignored, [k for k in ignored if _PERMISSIONISH.search(k)]


SUMMARY_HE_MAX = 1500


# The owner's task status summary ("סיכום מצב המשימה"): OPTIONAL, written by Codex (after a review) or Claude (interim) in the
# SAME call as their normal reply. Display-only: never used for any decision; a missing or malformed one is ignored, never fatal.
STATUS_SECTIONS = ("done", "checked", "found", "open")
STATUS_SUMMARY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": [*STATUS_SECTIONS, "bottom_line", "next"],
    "properties": {**{k: {"type": "array", "items": {"type": "string"}} for k in STATUS_SECTIONS},
                   "bottom_line": {"type": "string"}, "next": {"type": "string"}}}
STATUS_SUMMARY_RULE = (
    "status_summary_he: the Project Owner's status summary of the WHOLE task so far (not only this step), in clear, plain "
    "Hebrew, about 100-200 words in total, short items. done = the main completed activities; checked = investigations, "
    "experiments, tests or evidence reviewed; found = important findings, each starting with 'מאומת:' (verified from the "
    "evidence), 'טרם אומת:' (claimed but not independently reviewed) or 'השערה:' (provisional); open = unresolved issues, "
    "uncertainty and blockers; bottom_line = the most important current conclusion in 1-3 sentences; next = one concrete "
    "recommended next action. Research: the sources investigated, confidence and the current decision (e.g. GO / PIVOT / "
    "NO-GO / INCONCLUSIVE) if there is one. Code: the files changed, what works, the tests run and their results. Never "
    "invent results and never present a proposed conclusion as verified. If a previous summary is given, update it.")


def _clip_text(x: str, n: int) -> str:
    x = " ".join(x.split())
    return x if len(x) <= n else x[:n - 1].rstrip() + "…"


def valid_status_summary(v):
    """The normalised summary, or None if it is missing or malformed (then the previous summary simply stays)."""
    if not isinstance(v, dict):
        return None
    out = {}
    for k in STATUS_SECTIONS:
        items = v.get(k, [])
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            return None
        out[k] = [_clip_text(i, 400) for i in items if i.strip()][:8]
    for k, n in (("bottom_line", 800), ("next", 500)):
        if not isinstance(v.get(k, ""), str):
            return None
        out[k] = _clip_text(v.get(k, ""), n)
    text = " ".join([*sum((out[k] for k in STATUS_SECTIONS), []), out["bottom_line"], out["next"]])
    if not out["bottom_line"] or not re.search(r"[\u0590-\u05FF]", text):
        return None  # must be real Hebrew content with a bottom line
    return out


def valid_summary_he(v) -> bool:
    """`user_summary_he` is OPTIONAL plain-Hebrew text the agent writes in the SAME call, for the Project Owner only.
    It is display-only: never forwarded to the other agent and never used for any decision."""
    return isinstance(v, str) and len(v) <= SUMMARY_HE_MAX


MAX_CRITERIA, MAX_CRITERION_CHARS = 80, 1000  # a long research specification legitimately needs many detailed criteria


def validate_criteria(v) -> list:
    """Every criterion must be a real, distinct requirement. Each error says exactly what is wrong, so the planner can correct it."""
    def bad(msg):
        raise AdapterError("malformed", f"field 'acceptance_criteria' invalid: {msg}")
    if not isinstance(v, list):
        bad("it must be a JSON array of strings")
    if not v:
        bad("at least one acceptance criterion is required")
    if len(v) > MAX_CRITERIA:
        bad(f"{len(v)} items; the maximum is {MAX_CRITERIA}. Merge closely related requirements into fewer criteria "
            "without dropping any requirement")
    seen = {}
    for i, x in enumerate(v):
        if not isinstance(x, str):
            bad(f"item {i + 1} is not a string")
        if len(x) > MAX_CRITERION_CHARS:
            bad(f"item {i + 1} has {len(x)} characters; the maximum is {MAX_CRITERION_CHARS}")
        words = re.findall(r"\w{2,}", x)
        if len(x.strip()) < 12 or len(words) < 3 or re.fullmatch(r"(?i)\W*(tbd|todo|n/?a|none|placeholder|criterion \d+)\W*", x.strip()):
            bad(f"item {i + 1} is empty or not a meaningful, checkable requirement: {x.strip()[:60]!r}")
        key = " ".join(re.findall(r"\w+", x.lower()))  # same words, ignoring case and punctuation ("Section A" != "Section B")
        if key in seen:
            bad(f"item {i + 1} duplicates item {seen[key] + 1}")
        seen[key] = i
    return [x.strip() for x in v]


CAPABILITY_KINDS = ("ai_integration_script", "artifact_reviews")


def validate_capabilities(v) -> list:
    """Capabilities a plan says the mission will need (display + owner decision only; nothing is granted from here)."""
    out = []
    for x in (v if isinstance(v, list) else [])[:10]:
        if isinstance(x, dict) and x.get("kind") in CAPABILITY_KINDS:
            n = x.get("count")
            out.append({"kind": x["kind"], "path": str(x.get("path") or "")[:200], "reason": str(x.get("reason") or "")[:500],
                        "count": n if isinstance(n, int) and not isinstance(n, bool) and 0 < n <= 30 else 1})
    return out


def validate_plan(d: dict, max_instruction: int):
    ignored, perm = _split(d, ("plan", "acceptance_criteria", "instruction", "user_summary_he", "capabilities_needed"))
    crit = validate_criteria(d.get("acceptance_criteria"))
    return {"plan": _str(d, "plan", 8000), "acceptance_criteria": crit,
            "instruction": _str(d, "instruction", max_instruction, True),
            "capabilities_needed": validate_capabilities(d.get("capabilities_needed"))}, ignored, perm


AGENT_OPS = ("codex_review",)
ARTIFACT_VERDICTS = ("approved", "material_corrections", "minor_only", "insufficient_evidence")


def validate_agent_requests(v) -> tuple[list, list]:
    """Typed agent operations Claude asks the ORCHESTRATOR to run (it decides, budgets and executes them). -> (valid, rejected)"""
    ok, bad = [], []
    for x in (v if isinstance(v, list) else [])[:5]:
        if not isinstance(x, dict) or x.get("op") not in AGENT_OPS or not str(x.get("artifact") or "").strip():
            bad.append({"request": str(x)[:200], "reason": f"unknown operation or no artifact (supported: {', '.join(AGENT_OPS)})"})
            continue
        rid = re.sub(r"[^A-Za-z0-9_.-]", "", str(x.get("id") or ""))[:60] or f"req{len(ok) + 1}"
        ok.append({"op": x["op"], "artifact": str(x["artifact"]).strip()[:300], "focus": str(x.get("focus") or "")[:2000], "id": rid})
    if len(ok) > 3:
        bad += [{"request": r["id"], "reason": "at most 3 agent operations per step"} for r in ok[3:]]
        ok = ok[:3]
    return ok, bad


def validate_artifact_review(d: dict) -> dict:
    v = d.get("verdict")
    if v not in ARTIFACT_VERDICTS:
        raise AdapterError("malformed", f"artifact review verdict invalid: {str(v)[:40]!r}")
    out = {"verdict": v, "findings": _strlist(d, "findings", 40, 2000), "corrections": _str(d, "corrections", 8000)}
    if v == "material_corrections" and len(out["corrections"].strip()) < 10:
        raise AdapterError("malformed", "material_corrections needs concrete corrections")
    return out


def validate_report(d: dict, max_turns: int):
    known = ("summary", "changed_files", "commands_run", "remaining_issues", "session_id", "turns", "user_summary_he", "sources",
             "status_summary_he", "agent_requests")
    ignored, perm = _split(d, known)
    sid = d.get("session_id")
    if sid is not None and not (isinstance(sid, str) and _SESSION_RE.match(sid)):
        raise AdapterError("malformed", "session_id has an unsafe format")
    turns = d.get("turns", 0)
    if not isinstance(turns, int) or isinstance(turns, bool) or turns < 0:
        raise AdapterError("malformed", "turns invalid")
    if max_turns is not None and turns > max_turns:  # goal mode passes None: long research legitimately takes many turns
        raise AdapterError("process", f"executor used {turns} turns (limit {max_turns})")
    src = d.get("sources")
    sources = [{k: str(x.get(k, ""))[:500] for k in ("url", "retrieved_at", "used_for")} for x in src[:100] if isinstance(x, dict)] \
        if isinstance(src, list) else []
    L = REPORT_LIMITS
    return {"summary": _str(d, "summary", L["summary"]), "changed_files": _strlist(d, "changed_files", *L["changed_files"]),
            "sources": sources, "commands_run": _strlist(d, "commands_run", *L["commands_run"]),
            "remaining_issues": _strlist(d, "remaining_issues", *L["remaining_issues"]),
            "status_summary": valid_status_summary(d.get("status_summary_he")),
            "agent_requests": validate_agent_requests(d.get("agent_requests"))[0],
            "agent_requests_rejected": validate_agent_requests(d.get("agent_requests"))[1],
            "session_id": sid, "turns": turns}, ignored, perm


def validate_review(d: dict, max_instruction: int):
    from autonomy import OWNER_CATEGORIES, REVIEW_OUTCOMES
    ignored, perm = _split(d, ("verdict", "findings", "next_instruction", "question", "user_summary_he", "criteria_status",
                               "status_summary_he", "outcome", "owner_decision", "nonblocking"))
    verdict = d.get("verdict")
    if verdict not in VERDICTS:
        raise AdapterError("malformed", f"unknown verdict: {str(verdict)[:40]!r}")
    out = {"verdict": verdict, "findings": _strlist(d, "findings", 50, 2000),
           "next_instruction": _str(d, "next_instruction", max_instruction),
           "question": _str(d, "question", 2000), "status_summary": valid_status_summary(d.get("status_summary_he"))}
    cs = d.get("criteria_status")
    if isinstance(cs, list) and len(cs) <= MAX_CRITERIA and all(x in ("met", "not_met", "unverified") for x in cs):
        out["criteria_status"] = cs  # Codex's view; the completion gate also requires none to be open
    # structured outcome (optional for older/mock reviewers: derived from the verdict when absent)
    out["outcome"] = d.get("outcome") if d.get("outcome") in REVIEW_OUTCOMES else None
    od = d.get("owner_decision")
    if isinstance(od, dict) and od.get("category") not in (None, "", "none"):
        out["owner_decision"] = {k: str(od.get(k) or "")[:1500] for k in ("action", "purpose", "affected", "risks", "alternatives")}
        out["owner_decision"]["category"] = od["category"] if od["category"] in OWNER_CATEGORIES else "other"
    nb = d.get("nonblocking")  # optional, display-only: never a reason to reject a review
    out["nonblocking"] = [x[:1000] for x in nb if isinstance(x, str) and x.strip()][:30] if isinstance(nb, list) else []
    if verdict == "CONTINUE" and len(out["next_instruction"].strip()) < 10:
        raise AdapterError("malformed", "CONTINUE requires a specific next_instruction")
    if verdict == "HUMAN_REQUIRED" and not out["question"].strip():
        raise AdapterError("malformed", "HUMAN_REQUIRED requires a question")
    return out, ignored, perm


# --------------------------------------------------------------------------- interfaces
class PlannerAdapter:
    def plan(self, ctx: dict) -> str:
        raise NotImplementedError


class ExecutorAdapter:
    def execute(self, ctx: dict, fs: GuardedFS) -> str:
        raise NotImplementedError


class ReviewerAdapter:
    def review(self, ctx: dict) -> str:
        raise NotImplementedError


# --------------------------------------------------------------------------- mocks
class _Scripted:
    def __init__(self, steps, delay: float = 0.0):
        self.steps, self.calls, self.delay = list(steps), [], delay

    def _next(self):
        if self.delay:  # demo pacing only: lets a human watch the viewer update
            import time
            time.sleep(self.delay)
        if not self.steps:
            raise AdapterError("process", "mock scenario exhausted")
        step = self.steps.pop(0)
        if "error" in step:
            raise AdapterError(step["error"], step.get("message", "scripted mock error"))
        return step


class MockPlanner(_Scripted, PlannerAdapter):
    def plan(self, ctx):
        self.calls.append({"task": ctx["task"], "continuation": ctx.get("continuation")})
        step = self._next()
        return step["raw"] if "raw" in step else json.dumps(step, ensure_ascii=False)


class MockReviewer(_Scripted, ReviewerAdapter):
    def review(self, ctx):
        self.calls.append(ctx)
        step = self._next()
        return step["raw"] if "raw" in step else json.dumps(step, ensure_ascii=False)

    def review_part(self, ctx):  # part notes of a large research review: confirms the segments it was given
        self.calls.append(dict(ctx, part_review=True))
        return json.dumps({"segments_reviewed": [s["id"] for s in ctx["segments"]], "verified_claims": [],
                           "weak_or_unsupported_claims": [], "contradictions": [], "criteria_notes": [], "web_checks": []})


class MockExecutor(_Scripted, ExecutorAdapter):
    """Step: {"ops": [...], "report": {...}} | {"error": kind} | {"raw": "text"}.
    ops: write / delete / run go through the GuardedFS gateway.
    rogue_write / rogue_delete bypass it on purpose, to prove post-hoc detection works."""

    def execute(self, ctx, fs):
        self.calls.append({"instruction": ctx["instruction"], "session_id": ctx.get("session_id"),
                           "cycle": ctx["cycle"], "owner_notes": ctx.get("owner_notes"), "continuation_brief": ctx.get("continuation_brief")})
        step = self._next()
        ws = ctx["workspace"]
        touched = []
        for op in step.get("ops", []):
            kind = op["op"]
            if kind == "write":
                fs.write(op["path"], op["content"]); touched.append(op["path"])
            elif kind == "delete":
                fs.delete(op["path"]); touched.append(op["path"])
            elif kind == "run":
                fs.run(op["argv"])
            elif kind == "rogue_write":
                with open(f"{ws}/{op['path']}", "w", encoding="utf-8") as f:
                    f.write(op["content"])
                touched.append(op["path"])
            elif kind == "rogue_delete":
                import os
                os.remove(f"{ws}/{op['path']}"); touched.append(op["path"])
        if "raw" in step:
            return step["raw"]
        report = {"summary": "mock executor done", "changed_files": touched, "commands_run": [],
                  "remaining_issues": [], "session_id": "mock-session-1", "turns": 1}
        report.update(step.get("report", {}))
        return json.dumps(report, ensure_ascii=False)


def make_mock_agents(scenario: dict, delay: float = 0.0):
    return (MockPlanner(scenario.get("planner", []), delay), MockExecutor(scenario.get("executor", []), delay),
            MockReviewer(scenario.get("reviewer", []), delay))
