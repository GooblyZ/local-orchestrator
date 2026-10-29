"""Agent-to-agent conversation transcript (agent-neutral; used for Codex, Claude and mocks alike).

One JSONL record per message that was actually delivered between parties:
    {seq, ts, cycle, sender, recipient, kind, delivery, content, meta}
`content` is the exact text exchanged (only credentials are redacted, never summarised or reworded).
`delivery` is "complete" when the whole message arrived at once (e.g. a final CLI reply) or "streamed"
for adapters that deliver chunks. Hidden model reasoning is never recorded.
Ephemeral progress notes (activity) are NOT messages and are not stored here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from safety import Redactor
from store import now

PARTIES = ("user", "codex", "claude", "orchestrator", "harness")
KINDS = ("task", "plan", "instruction", "report", "verification", "review", "notice", "approval_request",
         "approval_decision", "control", "malformed_output", "final", "file_apply", "checkpoint", "continuation",
         "agent_result")
MAX_CONTENT = 400_000  # characters per message (a task may be up to 200,000)


def _last_seq(path: Path) -> int:
    """seq of the last COMPLETE record. The window grows until a whole line fits, so one very large message (a long task)
    can never make the numbering restart."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size, window = f.tell(), 65536
            while True:
                start = max(0, size - window)
                f.seek(start)
                lines = f.read().splitlines()
                if start > 0:
                    lines = lines[1:]  # the first line of a partial window may be cut
                for line in reversed(lines):
                    try:
                        return int(json.loads(line)["seq"])
                    except (ValueError, KeyError, TypeError):
                        continue
                if start == 0:
                    return 0
                window *= 4
    except OSError:
        pass
    return 0


class Transcript:
    def __init__(self, path, redactor: Redactor | None = None, listeners=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        self.listeners = list(listeners)

    def add(self, sender: str, recipient: str, kind: str, content, cycle=None, delivery="complete", **meta) -> dict:
        if sender not in PARTIES or recipient not in PARTIES or kind not in KINDS:
            raise ValueError(f"invalid message attribution: {sender}->{recipient} [{kind}]")
        if not isinstance(content, str):
            content = json.dumps(content, indent=2, ensure_ascii=False)
        text = self.redactor.text(content)
        if len(text) > MAX_CONTENT:
            text = text[:MAX_CONTENT] + f"\n...[truncated {len(text) - MAX_CONTENT} chars]"
        rec = {"seq": _last_seq(self.path) + 1, "ts": now(), "cycle": cycle, "sender": sender,
               "recipient": recipient, "kind": kind, "delivery": delivery, "content": text,
               "meta": self.redactor.obj(meta)}
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
        for fn in self.listeners:
            try:
                fn(rec)
            except Exception:  # a broken viewer must never break the task
                pass
        return rec


def read_transcript(path, start_offset: int = 0):
    """Return (messages, next_offset, skipped). Tolerates a truncated/garbled line (interrupted writes)."""
    msgs, skipped = [], 0
    try:
        with open(path, "rb") as f:
            f.seek(start_offset)
            data = f.read()
    except OSError:
        return [], start_offset, 0
    consumed = 0
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b"\n"):  # incomplete last line: leave it for the next read
            break
        consumed += len(raw)
        try:
            rec = json.loads(raw.decode("utf-8"))
            if not isinstance(rec, dict) or "seq" not in rec or "sender" not in rec:
                raise ValueError
            msgs.append(rec)
        except (ValueError, UnicodeDecodeError):
            skipped += 1
    return msgs, start_offset + consumed, skipped
