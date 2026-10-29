"""Shared test harness. Everything runs in temp directories with MOCK agents only."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mock_scenarios as ms  # noqa: E402
import orchestrator as orch  # noqa: E402
from agents import make_mock_agents  # noqa: E402

PY = sys.executable
UNITTEST_CMD = [PY, "-m", "unittest", "discover", "-s", "tests"]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.ws = ms.make_workspace(self.base / "ws")
        self.runs = self.base / "runs"
        self.lines: list[str] = []

    def cfg(self, **over):
        c = {"verify_command": UNITTEST_CMD, "runs_dir": str(self.runs)}
        c.update(over)
        return orch.load_config(None, c)

    def make(self, scenario, cfg_over=None, ws=None, **kw):
        ad = make_mock_agents(scenario)
        o = orch.Orchestrator(self.cfg(**(cfg_over or {})), ws or self.ws, self.runs, *ad,
                              out=self.lines.append, **kw)
        return o, ad

    def run_task(self, scenario, cfg_over=None, **kw):
        o, ad = self.make(scenario, cfg_over, **kw)
        return o, ad, o.start("implement calc.add and verify all tests")

    def events(self, o):
        p = o.run_dir / "events.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]

    def event_names(self, o):
        return [e["event"] for e in self.events(o)]

    def new_ws(self, name):
        return ms.make_workspace(self.base / name)
