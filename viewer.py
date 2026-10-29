"""Plain-terminal conversation viewer (no GUI, no server, standard library only).

Shows the exact messages stored in the transcript: sender, recipient, time, cycle, kind and content.
ASCII-only framing (safe on any Windows code page). Agent text is stripped of control characters so a
message cannot inject terminal escape sequences. JSON messages are pretty-printed for readability; the
stored transcript keeps the exact original text.
"""
from __future__ import annotations

import json
import re
import shutil
import textwrap
import time

from conversation import read_transcript

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_COLORS = {"codex": "36", "claude": "32", "orchestrator": "33", "user": "35", "harness": "34"}
_LABELS = {"harness": "TEST HARNESS"}


def _label(party: str) -> str:
    return _LABELS.get(party, party.upper())


def terminal_width(default=100) -> int:
    return max(60, min(140, shutil.get_terminal_size((default, 24)).columns))


def clean(text: str) -> str:
    return _CTRL.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    "))


def _scalar(v) -> str:
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v


def _render(obj, ind: int = 0) -> list[str]:
    """Readable block layout for JSON (multi-line strings shown as indented blocks, not \\n escapes)."""
    pad = " " * ind
    out: list[str] = []
    items = obj.items() if isinstance(obj, dict) else [(None, v) for v in obj]
    for k, v in items:
        label = f"{k}:" if k is not None else "-"
        if isinstance(v, (dict, list)) and v:
            out.append(f"{pad}{label}")
            out += _render(v, ind + 2)
        elif isinstance(v, str) and "\n" in v:
            out.append(f"{pad}{label} |")
            out += [" " * (ind + 2) + ln for ln in v.rstrip("\n").split("\n")]
        else:
            shown = "[]" if v == [] else "{}" if v == {} else _scalar(v)
            out.append(f"{pad}{label} {shown}")
    return out


def _body(content: str) -> str:
    text = clean(content)
    try:
        obj = json.loads(text)
        if isinstance(obj, (dict, list)) and obj:
            return clean("\n".join(_render(obj)))
    except ValueError:
        pass
    return text


def _wrap(line: str, width: int) -> list[str]:
    if len(line) <= width:
        return [line]
    lead = line[:len(line) - len(line.lstrip(" "))]
    return textwrap.wrap(line, width=width, subsequent_indent=lead + "  ", break_long_words=True,
                         break_on_hyphens=False, drop_whitespace=False) or [""]


def stamp(ts: str) -> str:
    return ts.replace("T", " ")[:19] + "Z" if ts else "?"


def format_message(m: dict, width: int = 100, color: bool = False, show_prompt: bool = False) -> list[str]:
    s, r = str(m.get("sender", "?")), str(m.get("recipient", "?"))
    cyc = m.get("cycle")
    head = (f"+-- #{m.get('seq')}  cycle {cyc if cyc is not None else '-'}  {stamp(m.get('ts', ''))}  "
            f"{_label(s)} -> {_label(r)}  [{m.get('kind')}]"
            + ("  (streamed)" if m.get("delivery") == "streamed" else ""))
    head = clean(head)[:width]
    if color:
        head = f"\x1b[{_COLORS.get(s, '37')};1m{head}\x1b[0m"
    lines = [head]
    for raw_line in _body(m.get("content", "")).split("\n"):
        lines += ["| " + piece for piece in _wrap(raw_line, width - 2)]
    prompt = (m.get("meta") or {}).get("delivered_prompt")
    if show_prompt and prompt:
        lines.append("| ---- full prompt delivered to the agent (framing + instruction) ----")
        for raw_line in clean(prompt).split("\n"):
            lines += ["| " + piece for piece in _wrap(raw_line, width - 2)]
    lines.append("+--")
    return lines


def format_activity(a: dict, width: int = 100) -> str:
    return clean(f"  .. {stamp(a.get('ts', ''))[11:]} {a.get('source', '?')}: {a.get('text', '')}")[:width]


class Viewer:
    def __init__(self, out=print, width: int | None = None, color: bool = False, show_activity: bool = True,
                 show_prompts: bool = False):
        self.out, self.width, self.color, self.show_activity = out, width or terminal_width(), color, show_activity
        self.show_prompts = show_prompts

    def message(self, rec: dict) -> None:
        for line in format_message(rec, self.width, self.color, self.show_prompts):
            self.out(line)

    def activity(self, rec: dict) -> None:
        if self.show_activity:
            self.out(format_activity(rec, self.width))


def select(msgs, cycle=None, seq=None):
    return [m for m in msgs if (cycle is None or m.get("cycle") == cycle) and (seq is None or m.get("seq") == seq)]


def replay(path, viewer: Viewer, delay: float = 0.0, cycle=None, seq=None, sleep=time.sleep) -> int:
    msgs, _, skipped = read_transcript(path)
    chosen = select(msgs, cycle, seq)
    for i, m in enumerate(chosen):
        if i and delay:
            sleep(delay)
        viewer.message(m)
    if skipped:
        viewer.out(f"(note: {skipped} unreadable transcript line(s) skipped)")
    return len(chosen)


def watch(path, viewer: Viewer, is_active, poll: float = 0.5, sleep=time.sleep, max_idle_polls=None) -> int:
    """Follow a transcript while the task is active (run this in a second terminal)."""
    offset, shown, idle = 0, 0, 0
    while True:
        active = is_active()
        msgs, offset, _ = read_transcript(path, offset)
        for m in msgs:
            viewer.message(m)
            shown += 1
        idle = 0 if msgs else idle + 1
        if not active and not msgs:
            return shown
        if max_idle_polls is not None and idle >= max_idle_polls:
            return shown
        if not msgs:
            sleep(poll)
