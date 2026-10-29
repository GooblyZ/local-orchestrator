"""Deterministic run policy shared by the orchestrator and the launcher (no model is ever consulted here).

- Which executors may have their output EXECUTED on this machine (only scripted mocks / simulators).
- Per-run model-call budgets.
- How an agent error maps to an honest final status.
- The compact evidence carried into the next correction cycle.
"""
from __future__ import annotations

from agents import MockExecutor
from safety import clip

# Honest final statuses:
#   COMPLETE        verified by the deterministic gate AND approved by the reviewer
#   NEEDS_REVISION  work exists but was not accepted within the cycle / call budget
#   FAILED          the task could not be done (reviewer said so, malformed output, no progress)
#   BLOCKED         an external precondition failed (authentication, quota, configuration) - nothing to retry here
#   HUMAN_REQUIRED  waiting for the owner (approval, question, policy violation)
#   STOPPED         cancelled by the owner
BLOCKING_ERRORS = {"auth", "quota", "config"}

DYNAMIC_EXECUTION_BLOCKED = (
    "Local execution was not accepted: with a real Claude Code agent the task either runs in the NATIVE profile, where "
    "Claude reads, edits and runs the project's tests on this machine under your Windows permissions (accept with "
    "`start` or `run --accept-local-execution`; same general risks as using Claude Code normally), or in the RESTRICTED "
    "profile (`--allow-create`: no tools, static checks only). Nothing was started.")


def executor_is_trusted(executor) -> bool:
    """True only for executors whose output is scripted by us (MOCK agents, the simulated Claude CLI). Code produced
    by a real model is untrusted and must never be executed outside an OS sandbox."""
    if isinstance(executor, MockExecutor):
        return True
    cli = getattr(executor, "cli", None)
    return bool(getattr(getattr(cli, "runner", None), "simulated", False))


def changed_file_contents(root, diff: dict, redactor, max_files: int = 10, max_chars: int = 15000) -> list[dict]:
    """The REAL content of files added/modified in this cycle, so the reviewer judges the code, not the agent's claims."""
    import os
    from pathlib import Path
    out = []
    for status in ("added", "modified"):
        for rel in diff.get(status, []):
            if len(out) >= max_files:
                return out
            p = Path(root) / rel
            if os.path.islink(p) or not p.is_file():
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                out.append({"path": rel, "status": status, "content": "(binary file, not shown)"})
                continue
            out.append({"path": rel, "status": status, "content": clip(redactor.text(data.decode("utf-8", "replace")), max_chars)})
    return out


def budget_limits(cfg: dict) -> dict:
    """Model calls allowed in ONE run (counted before each call, persisted, so resume cannot reset them)."""
    return {"codex": cfg["max_codex_calls"] or 1 + cfg["max_cycles"],
            "claude": cfg["max_claude_calls"] or cfg["max_cycles"]}


def status_for_error(kind: str) -> str:
    if kind in BLOCKING_ERRORS:
        return "BLOCKED"
    if kind == "cancelled":
        return "STOPPED"
    return "FAILED"


def compact_evidence(cycle: int, evidence: dict) -> dict:
    """What the executor is told about the previous cycle, straight from the record (not from any agent's summary)."""
    ver = evidence.get("verification") or {}
    out = {"cycle": cycle, "verification_status": ver.get("status"), "executed": ver.get("executed", ver.get("mode") != "static"),
           "problems": clip(str(ver.get("output_tail") or ""), 2000), "required_defs": ver.get("required_defs")}
    fa = evidence.get("file_apply")
    if fa:
        out["file_proposal"] = {"status": fa.get("status"), "reasons": [clip(str(r), 300) for r in fa.get("reasons", [])][:20],
                                "written": [w.get("path") for w in fa.get("written", [])]}
    return out
