"""Delegated autonomy: the deterministic policy that decides what the agents may settle themselves.

The owner authorizes a bounded mission once (scope, budget, autonomy level). Inside it, ordinary technical decisions are
made by Codex and Claude; this module - never a model - checks that every such decision stays inside the authorization.
A model's words ("approved", "the owner agreed") are never an authorization: only the owner's own actions in the app or
on the command line change the persisted policy.

Operation classes:
  CLASS 1  automatic: reading the project, editing project code, running the project's tests through the capability
           gateway, repeating them, debugging, task-local reports.
  CLASS 2  autonomous decision, validated here: choosing an approach, answering review findings, retrying, falling back to
           an owner-authorized model, deciding that more review has diminishing value, running a SCREENED project script.
  CLASS 3  owner authorization: money / paid tiers, credentials, scope outside the authorized folder, destructive changes to
           valuable data, external publication or effects (push, deploy, upload, messages), system changes, disabling
           security controls, and anything whose risk the environment cannot contain.
"""
from __future__ import annotations

import re

LEVELS = ("supervised", "balanced", "autonomous")
DEFAULT_LEVEL = "balanced"

# What a Codex "owner decision" may be about. Anything else is a technical matter the agents settle themselves.
OWNER_CATEGORIES = ("spending", "credentials", "scope_expansion", "destructive_data", "external_publication",
                    "external_effect", "system_change", "security_control")
REVIEW_OUTCOMES = ("approved", "material_corrections", "minor_only", "insufficient_evidence", "owner_decision_required",
                   "failed")
OUTCOME_OF_VERDICT = {"COMPLETE": "approved", "CONTINUE": "material_corrections", "HUMAN_REQUIRED": "owner_decision_required",
                      "FAILED": "failed"}

_SETTINGS = {  # per level; the owner sees these in plain words (web/index.html AUTONOMY_HE)
    "supervised": {"codex_decides_technical": False, "redirect_blocked_commands": False, "scripts": "owner",
                   "transient_retries": 1, "review_retries": 0, "model_fallback": False, "max_redirects": 0,
                   "max_polish_rounds": 2, "review_failure_pauses": True, "single_pass_corrections": 0,
                   "terminate_on_stall": False},
    "balanced": {"codex_decides_technical": True, "redirect_blocked_commands": True, "scripts": "screened",
                 "transient_retries": 2, "review_retries": 1, "model_fallback": True, "max_redirects": 2,
                 "max_polish_rounds": 3, "review_failure_pauses": True, "single_pass_corrections": 2,
                 "terminate_on_stall": True},
    "autonomous": {"codex_decides_technical": True, "redirect_blocked_commands": True, "scripts": "screened",
                   "transient_retries": 3, "review_retries": 2, "model_fallback": True, "max_redirects": 3,
                   "max_polish_rounds": 4, "review_failure_pauses": True, "single_pass_corrections": 3,
                   "terminate_on_stall": True},
}
# Tasks created before autonomy levels existed keep EXACTLY their old behaviour (no automatic retries, no polishing-loop
# stop, a failed code review ends the task) - only the capability gateway is offered, with scripts needing the owner.
_LEGACY = dict(_SETTINGS["supervised"], level="legacy", transient_retries=0, max_polish_rounds=None, review_failure_pauses=False)


MAX_SCRIPT_AI_RUNS = 20


AGENT_REVIEWS_DEFAULT = {"supervised": 0, "balanced": 6, "autonomous": 10}
_AI_SCRIPT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,150}\.py$")


def make_autonomy(level: str | None = None, fallback_models: dict | None = None, script_ai_runs: int = 0,
                  agent_reviews: int | None = None, ai_scripts: list | None = None) -> dict:
    """The persisted, owner-authorized policy of one task. Raises ValueError for anything not offered.
    script_ai_runs: how many times project scripts may call the Claude/Codex CLIs through the owner's EXISTING subscriptions
    (e.g. the product's own AI features in a smoke test), only with the task's authorized models; 0 = always ask."""
    level = level or DEFAULT_LEVEL
    if level not in LEVELS:
        raise ValueError(f"autonomy must be one of {LEVELS}")
    if not isinstance(script_ai_runs, int) or isinstance(script_ai_runs, bool) or not 0 <= script_ai_runs <= MAX_SCRIPT_AI_RUNS:
        raise ValueError(f"script_ai_runs must be 0-{MAX_SCRIPT_AI_RUNS}")
    agent_reviews = AGENT_REVIEWS_DEFAULT[level] if agent_reviews is None else agent_reviews
    if not isinstance(agent_reviews, int) or isinstance(agent_reviews, bool) or not 0 <= agent_reviews <= 30:
        raise ValueError("agent_reviews must be 0-30")
    ai_scripts = list(ai_scripts or [])
    if len(ai_scripts) > 10 or not all(isinstance(x, str) and _AI_SCRIPT.match(x) and ".." not in x.split("/") for x in ai_scripts):
        raise ValueError("ai_scripts must be project-relative .py paths (forward slashes, at most 10)")
    fb = {"codex": [], "claude": []}
    for agent, models in (fallback_models or {}).items():
        if agent not in fb or not isinstance(models, list) or len(models) > 3:
            raise ValueError("fallback_models must be {codex: [...], claude: [...]} with at most 3 each")
        for m in models:
            if not isinstance(m, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", m):
                raise ValueError("fallback model names must be model ids")
        fb[agent] = list(models)
    return {"level": level, "fallback_models": fb, "script_ai_runs": script_ai_runs, "agent_reviews": agent_reviews,
            "ai_scripts": ai_scripts, **_SETTINGS[level]}


def from_settings(s: dict, offered: dict) -> dict:
    """The owner's mission-setup choices (browser form) -> validated settings + the persisted policy. Models must be ones the
    installed CLIs offer (never a paid tier). Raises ValueError."""
    fb = {"codex": list(s.get("fallback_codex") or []), "claude": list(s.get("fallback_claude") or [])}
    for agent, models in fb.items():
        if not set(models) <= set(offered.get(agent) or []):
            raise ValueError(f"fallback_{agent} must list models the installed {agent} CLI offers")
    level = s.get("autonomy") or DEFAULT_LEVEL
    reviews = s.get("agent_reviews")
    auto = make_autonomy(level, fb, s.get("script_ai_runs", 0), reviews if reviews not in (None, "") else None, s.get("ai_scripts") or [])
    return {"autonomy": auto["level"], "fallback_codex": fb["codex"], "fallback_claude": fb["claude"],
            "script_ai_runs": auto["script_ai_runs"], "agent_reviews": auto["agent_reviews"], "ai_scripts": auto["ai_scripts"]}


def policy_from_settings(st: dict) -> dict:
    return make_autonomy(st["autonomy"], {"codex": st["fallback_codex"], "claude": st["fallback_claude"]}, st["script_ai_runs"],
                         st.get("agent_reviews"), st.get("ai_scripts") or [])


def policy(s: dict) -> dict:
    """The task's policy, from its persisted owner choice; older tasks get the legacy (pre-autonomy) behaviour."""
    a = s.get("autonomy")
    if isinstance(a, dict) and a.get("level") in LEVELS:
        return {**_SETTINGS[a["level"]], "script_ai_runs": 0, "agent_reviews": AGENT_REVIEWS_DEFAULT[a["level"]], "ai_scripts": [],
                **{k: v for k, v in a.items() if k in ("level", "fallback_models", "script_ai_runs", "agent_reviews", "ai_scripts")}}
    return {**_LEGACY, "fallback_models": {"codex": [], "claude": []}, "script_ai_runs": 0, "agent_reviews": 0, "ai_scripts": []}


def authorized_models(s: dict, pol: dict) -> dict:
    """Models the owner authorized for this task: the ones it was started with plus the owner's fallbacks."""
    a = s.get("agent_settings") or {}
    out = {}
    for agent, key in (("codex", "codex_model"), ("claude", "claude_model")):
        ms = [m for m in [a.get(key)] + list(pol["fallback_models"].get(agent) or []) if m]
        out[agent] = list(dict.fromkeys(ms))
    return out


def ai_runs_used(audit_path) -> int:
    """Project-script AI CLI runs already made in this task (counted from the gateway's own audit log)."""
    import json
    from pathlib import Path
    p = Path(audit_path)
    if not p.exists():
        return 0
    n = 0
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            n += bool(json.loads(ln).get("ai_cli_run"))
        except ValueError:
            continue
    return n


# --------------------------------------------------------------------------- owner-level detection (conservative)
# Used to catch an owner-level matter even when an agent labelled it "technical". False positives only cause an owner
# question (the safe direction); this is never used to GRANT anything.
_OWNER_WORDS = re.compile(
    r"\b(git\s+push|push(ed|ing)?\s+to|deploy\w*|publish\w*|release\s+to|upload\w*|purchas\w*|pay(ment|ing)?|billing|"
    r"paid\s+(api|tier|plan)|credit\s*card|subscription\s+(upgrade|change)|credential\w*|password\w*|api[\s_-]?key|"
    r"secret\w*|access\s+tokens?|api\s+tokens?|log\s*in\s+to|sign\s*(in|up)\s+(to|for|with)|send\s+(an?\s+)?(e-?mail|message)|e-?mail\s+to|post\s+to|instagram|"
    r"production\s+(data|database|server)|drop\s+table|delete\s+(the\s+)?(data|database|backup)|wipe|format\s+the|"
    r"outside\s+(the\s+)?(project|authori[sz]ed)|system\s+(setting|configuration)|registry|install\w*\s+(system|globally|"
    r"winget|choco)|disable\s+(the\s+)?(security|sandbox|guard)|google\s+flow|credits?\s+(balance|usage|spend\w*))\b"
    r"|תשלום|לשלם|חיוב|כרטיס\s*אשראי|מנוי|סיסמ|אסימון|הרשמה|להתחבר|לפרסם|פרסום|העלאה|להעלות|פריסה|לפרוס|למחוק|מחיקת|"
    r"מסד\s*הנתונים\s*החי|נתוני\s*ייצור|מחוץ\s*לתיקי|הגדרות\s*מערכת|קרדיט|לשלוח|דוא\"?ל|אינסטגרם", re.I)


def looks_owner_level(*texts) -> bool:
    return any(_OWNER_WORDS.search(str(t or "")) for t in texts)


# Model questions and the product's own AI-CLI runs: routine when the owner pre-authorized them, never when anything in the
# request is a hard owner matter (money beyond the existing subscriptions, credentials, publication, deletion, installs...).
_MODEL_Q = re.compile(r"/model\b|\bwhich\s+model|\bmodel\s*(id|identifier|name)s?\b|available\s+models?|supported\s+models?|"
                      r"מזהה\s*(ה)?מודל|איזה\s*מודל|מודל\s*(זמין|שנתמך|נתמך)", re.I)
_AI_CALL = re.compile(r"\b(codex|claude)\b|\bai[_ -]?(smoke|review|cli|call)|קריאת\s*(codex|claude)|פני(ה|ות)\s*ל-?\s*(codex|claude)", re.I)
_HARD_OWNER = re.compile(r"git\s+push|deploy\w*|publish\w*|upload\w*|purchas\w*|payment|paid\s+(api|tier|plan)|api[\s_-]?key|"
                         r"credit\s*card|password|credential|secret|\bdelete\b|drop\s+table|production\s+(data|database|server)|"
                         r"instagram|post\s+to|send\s+(an?\s+)?(e-?mail|message)|install\w*|registry|disable|google\s+flow|"
                         r"לפרסם|פרסום|פריסה|לפרוס|תשלום|לשלם|כרטיס\s*אשראי|סיסמ|למחוק|מחיקת|להעלות|העלאה|אינסטגרם|בתשלום|להתקין|התקנ", re.I)
AI_AUTH_WHY = ("running the product's own AI features calls the Claude/Codex CLIs on the owner's subscription; the owner has not "
               "pre-authorized such runs for this task (set 'AI calls by project scripts' and resume)")


def outcome_of(rv: dict) -> str:
    o = rv.get("outcome")
    return o if o in REVIEW_OUTCOMES else OUTCOME_OF_VERDICT.get(rv.get("verdict"), "material_corrections")


def review_route(rv: dict, pol: dict, reasks_used: int) -> tuple[str, str]:
    """What happens with a validated review. Returns (route, why):
      owner     - a genuine owner decision (Class 3), or the owner chose 'supervised'
      reask     - Codex escalated a TECHNICAL matter: it is asked once to decide it itself
      complete  - approval candidate (the deterministic completion gate still applies)
      continue  - corrections go to Claude automatically
      failed    - Codex declared the task impossible
    """
    verdict, outcome = rv.get("verdict"), outcome_of(rv)
    od = rv.get("owner_decision") or {}
    if verdict == "FAILED" or outcome == "failed":
        return "failed", "reviewer declared failure"
    wants_owner = verdict == "HUMAN_REQUIRED" or outcome == "owner_decision_required"
    if wants_owner:
        texts = (rv.get("question"), od.get("action"), od.get("purpose"), od.get("affected"), od.get("risks"))
        if not pol["codex_decides_technical"]:
            return "owner", ("legacy task (created before autonomy levels; behaves as supervised): every escalation goes to "
                             "the owner - choose an autonomy level to change this" if pol["level"] == "legacy"
                             else "supervised mission: every escalation goes to the owner")
        joined = " ".join(str(x or "") for x in texts)
        if (reasks_used < 1 and od.get("category") in (None, "", "none", "other", "external_effect", "spending")
                and not _HARD_OWNER.search(joined)):
            if _AI_CALL.search(joined) and pol.get("ai_runs_left", 0) > 0:
                return "reask", (f"the owner pre-authorized {pol['ai_runs_left']} more AI CLI run(s) by project scripts "
                                 "(existing subscriptions, authorized models only, enforced by the capability gateway)")
            if _AI_CALL.search(joined) and _MODEL_Q.search(joined):
                return "owner", AI_AUTH_WHY
            if _MODEL_Q.search(joined):
                return "reask", "choosing a model is a technical decision; the authorized and offered models are listed"
        if od.get("category") in OWNER_CATEGORIES:
            return "owner", f"owner-level category: {od['category']}"
        if looks_owner_level(*texts):
            return "owner", "the request mentions an owner-level action (money, credentials, publication, data, scope)"
        if reasks_used >= 1:
            return "owner", "Codex escalated a technical matter twice; the owner is told it could not be settled"
        return "reask", "technical matter inside the authorized mission: Codex decides it itself"
    if verdict == "COMPLETE" or outcome == "approved":
        return "complete", "reviewer approved"
    if outcome == "minor_only" and pol["level"] != "supervised":
        return "complete", "only minor, non-blocking suggestions remain"
    return "continue", f"review outcome {outcome}: corrections go to Claude"


REASK_NOTE = """POLICY (set by the orchestrator, not negotiable): your previous review escalated to the project owner, but the
matter is a TECHNICAL decision inside the mission the owner already authorized (autonomy level: {level}). The owner is not a
messenger between the agents. Decide it yourself: pick the approach the evidence supports, and return CONTINUE with a
concrete next_instruction for Claude, or COMPLETE / FAILED. Pre-authorized without asking anybody: reading and editing the
project, running its tests and diagnostics, retrying, choosing among valid approaches, and running project scripts through
the orchestrator's capability gateway (scripts are screened automatically; ones with external effects are stopped for the
owner by the orchestrator itself). Use HUMAN_REQUIRED only for an owner-level matter and then fill owner_decision with one
of these categories: {categories}.
Your escalated question was: {question}"""


def models_note(pol: dict) -> str:
    """Facts for Codex (read without any model call): the authorized models, the CLI catalog, the AI-run allowance."""
    m, offered = pol.get("models") or {}, pol.get("offered_models") or {}
    return ("\nMODELS (from the orchestrator, no model call was needed): authorized for this task - Codex: "
            f"{', '.join(m.get('codex') or []) or 'none'}; Claude: {', '.join(m.get('claude') or []) or 'CLI default'}. "
            f"Offered by the installed Codex CLI catalog (only the authorized ones may be used): {', '.join(offered.get('codex') or []) or 'unknown'}. "
            f"Project-script AI CLI runs pre-authorized and left: {pol.get('ai_runs_left', 0)} (a script must be given "
            "`--model <authorized id>`; the gateway refuses other models). Never ask the owner to look up models in a CLI menu.")


def reask_note(rv: dict, pol: dict) -> str:
    return REASK_NOTE.format(level=pol["level"], categories=", ".join(OWNER_CATEGORIES),
                             question=str(rv.get("question") or "")[:2000]) + models_note(pol)


POLICY_FOR_REVIEWER = """MISSION AUTONOMY POLICY (from the orchestrator): the owner authorized this mission once; autonomy level {level}.
Decide technical matters yourself (approach, retries, whether a correction is needed, whether another review round is
worthwhile, running the project's tests or project scripts through the capability gateway). Report the result with
`outcome`: approved | material_corrections (Claude must fix; it is routed to Claude automatically) | minor_only (only
non-blocking polish; does not prevent completion) | insufficient_evidence (say exactly what evidence is missing) |
owner_decision_required | failed. Use HUMAN_REQUIRED/owner_decision_required ONLY for: {categories}; then fill
owner_decision (action, purpose, affected resources, risks, alternatives). Otherwise owner_decision.category = "none".
Prefer verified product progress over cosmetic report or documentation revisions. Skipped tests are not evidence."""


def reviewer_policy_note(pol: dict) -> str:
    return POLICY_FOR_REVIEWER.format(level=pol["level"], categories=", ".join(OWNER_CATEGORIES)) + models_note(pol)


# --------------------------------------------------------------------------- blocked shell commands
_CLASS3_CMD = re.compile(r"\b(git\s+(push|remote|config|reset|clean|checkout\s+--)|rm|rmdir|del|erase|rd|format|curl|wget|"
                         r"Invoke-WebRequest|iwr|Invoke-RestMethod|scp|ssh|ftp|pip3?\s+install|npm\s+(publish|install\s+-g)|"
                         r"winget|choco|reg(\.exe)?\s|setx|shutdown|taskkill|icacls|takeown|powershell|pwsh|cmd(\.exe)?\s+/c|"
                         r"mklink|net\s+user|sc\s+|schtasks|gh\s+(pr|release|repo)|vercel|netlify|docker\s+push)\b", re.I)
_ROUTINE_PARTS = re.compile(r"^\s*(?:do\s+)?(cd\s+\S.*|python3?(\s+-I)?\s+-m\s+(unittest|pytest)\b.*|"
                            r"py(thon3?)?\s+(-I\s+)?[\w./\\:\"-]+\.py\b.*|pytest\b.*|ls\b.*|dir\b.*|cat\s.*|head\b.*|tail\b.*|"
                            r"grep\b.*|findstr\b.*|wc\b.*|sort\b.*|echo\b.*|pwd|true|false|for\s+\w+\s+in\s+[\w\s.-]*|done|"
                            r"sleep\s+\d+|python3?\s+--version|which\s.*|where\s.*)\s*$", re.I)


def classify_denial(d: dict) -> str:
    """'routine' (a test / project-script / read-only composition the capability gateway covers), 'owner' (Class 3 or
    unknown). Only Bash denials can be routine; denied file or web access outside the rules always goes to the owner."""
    if str(d.get("tool")) != "Bash":
        return "owner"
    cmd = str(d.get("input") or "")
    plain = re.sub(r"\d?>\s*/dev/null|2>&1", " ", cmd)
    if not cmd.strip() or _CLASS3_CMD.search(cmd) or re.search(r"[`]|\$\(|>|<<", plain):
        return "owner"  # command substitution, output redirection to files and here-docs are never "routine"
    unquoted = re.sub(r'"[^"]*"|\'[^\']*\'', '"q"', cmd)  # operators inside quotes (e.g. a grep pattern) are text
    parts = [p for p in re.split(r"&&|\|\||;|\|", unquoted) if p.strip()]
    if parts and all(_ROUTINE_PARTS.match(p) for p in parts):
        return "routine"
    return "owner"


def redirect_note(denied: list[dict], gateway_cmd: str) -> str:
    cmds = "; ".join(str(d.get("input") or d.get("tool"))[:200] for d in denied)
    return (f"ORCHESTRATOR (automatic, no owner involved): these composed shell commands were blocked by the permission "
            f"rules: {cmds}. Do not retry them and do not look for another shell workaround. Use the structured capability "
            f"gateway instead - it runs in the project folder, needs no cd, reports the exit code and can repeat, tail and "
            f"filter output itself:\n  {gateway_cmd} test [--target MODULE_OR_PATH ...] [--repeat N] [--tail N] [--grep REGEX]\n"
            f"  {gateway_cmd} script RELATIVE/PATH.py [--arg=VALUE ...]\n  {gateway_cmd} env [--tool NAME ...] [--module NAME ...]\n"
            "Then finish the previous instruction.")


# --------------------------------------------------------------------------- failure classification
_TRANSIENT = re.compile(r"timed? ?out|timeout|connection (reset|refused|aborted|closed)|temporar|try again|ECONNRESET|"
                        r"EPIPE|overloaded|\b50[234]\b|service unavailable|internal server error|stream (error|disconnected)|"
                        r"network error|socket|exited with code|no turn\.completed|cannot start", re.I)
_MODEL_UNAVAILABLE = re.compile(r"model.{0,60}(not supported|not available|unavailable|does not exist|not found)|"
                                r"unknown model|unsupported model|no access to model|model_not_found", re.I)
_SERVICE_TIER = re.compile(r"service[_ ]?tier", re.I)
_TOOL_MISSING = re.compile(r"(ModuleNotFoundError|No module named|command not found|is not recognized as an internal|"
                           r"not installed|executable not found)", re.I)


def classify_failure(kind: str, message: str) -> dict:
    """An adapter error -> {class, recoverable, diagnosis}. Diagnoses never propose silent edits of the owner's configuration."""
    m = str(message or "")
    if kind == "auth":
        return {"class": "authentication", "recoverable": False,
                "diagnosis": "The CLI is not signed in with the subscription. Sign in again (codex login / claude), then resume."}
    if kind == "quota":
        return {"class": "rate_or_subscription_limit", "recoverable": False,
                "diagnosis": "The subscription's usage limit was reached. Resume after the limit resets; no paid option is used."}
    if kind == "permission":
        return {"class": "permission_denied", "recoverable": True, "diagnosis": "A tool use was outside the permission rules."}
    if kind == "cancelled":
        return {"class": "cancelled", "recoverable": False, "diagnosis": "Stopped by the owner or by the owner's budget."}
    if _SERVICE_TIER.search(m):
        return {"class": "configuration_incompatible", "recoverable": False,
                "diagnosis": "Codex reported a service_tier setting or speed-tier signal. Remove any service_tier line from "
                             "the orchestrator's isolated Codex config.toml (the orchestrator never edits it), then resume."}
    if _MODEL_UNAVAILABLE.search(m):
        return {"class": "model_unavailable", "recoverable": True,
                "diagnosis": "The selected model is not available to this subscription/CLI version. Only a fallback model "
                             "the owner authorized for this task may be used; otherwise choose another model and resume."}
    if kind == "config":
        return {"class": "configuration_incompatible", "recoverable": False,
                "diagnosis": "The CLI configuration check failed before any model call: " + m[:300]}
    if kind == "malformed":
        return {"class": "malformed_output", "recoverable": True, "diagnosis": "An answer failed format validation."}
    if _TOOL_MISSING.search(m):
        return {"class": "missing_tool", "recoverable": False, "diagnosis": "A required tool or module is missing: " + m[:200]}
    if kind in ("timeout", "process") and _TRANSIENT.search(m) and "unexpected tool activity" not in m and "too large" not in m:
        return {"class": "transient", "recoverable": True, "diagnosis": "A temporary process or connection error."}
    return {"class": "unrecoverable_process_error", "recoverable": False, "diagnosis": m[:300]}


def backoff_seconds(attempt: int) -> float:
    return min(60.0, 5.0 * (3 ** (attempt - 1)))  # 5 s, 15 s, 45 s


# --------------------------------------------------------------------------- review loops
def review_loop_reason(s: dict, ver: dict, outcome: str, criteria_status, pol: dict) -> str | None:
    """Tests already pass but reviews keep asking for more without any criterion moving to 'met': stop the polishing loop."""
    if not pol.get("max_polish_rounds") or not criteria_status:  # no criteria signal: the stall detection covers it
        return None
    st = s.setdefault("polish", {"rounds": 0, "met": -1})
    met = sum(1 for x in criteria_status or [] if x == "met")
    if not ver.get("passed") or outcome not in ("material_corrections", "minor_only", "insufficient_evidence"):
        s["polish"] = {"rounds": 0, "met": met}
        return None
    if met > st["met"]:
        s["polish"] = {"rounds": 1, "met": met}
        return None
    st["rounds"] += 1
    if st["rounds"] > pol["max_polish_rounds"]:
        return (f"{st['rounds']} review rounds after the tests passed without any additional acceptance criterion being met "
                "(diminishing returns)")
    return None


def repeated_findings(s: dict, rv: dict) -> bool:
    """True when Codex returns the SAME material findings and next step as in its previous review (normalized text), i.e.
    Claude's correction did not change the reviewer's assessment. The key is kept in the task state."""
    import hashlib
    norm = lambda x: re.sub(r"\s+", " ", str(x or "")).strip().lower()
    key = hashlib.sha256(("\n".join(sorted(norm(f) for f in rv.get("findings") or [])) + "\n#" + norm(rv.get("next_instruction"))
                          ).encode("utf-8")).hexdigest()
    prev, s["last_review_key"] = s.get("last_review_key"), key
    return prev == key

# --------------------------------------------------------------------------- audit of autonomous decisions
def record(s: dict, kind: str, what: str, why: str, **extra) -> dict:
    """A decision the system made WITHOUT the owner, kept in the task state (last 100) for the owner to inspect."""
    from store import now
    d = {"at": now(), "cycle": s.get("cycle"), "kind": kind, "what": str(what)[:600], "why": str(why)[:400], **extra}
    s["decisions"] = (s.get("decisions") or [])[-99:] + [d]
    m = s.setdefault("metrics", {})
    m[kind] = m.get(kind, 0) + 1
    return d


def bump(s: dict, key: str, n: int = 1) -> None:
    m = s.setdefault("metrics", {})
    m[key] = m.get(key, 0) + n
