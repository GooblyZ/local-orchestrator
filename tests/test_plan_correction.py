"""Codex planning output that fails format validation: precise errors, bounded correction requests (never infinite, always
counted against the owner's budget), no fabricated or empty criteria. Reproduces the failed live research run, whose plan
had 51 valid criteria against an old limit of 50. Real Codex adapter against a SIMULATED CLI; no model is contacted."""
import json
import unittest

from helpers import ms, orch  # noqa: F401
from test_app import OK_REVIEW, WRITE_GOOD
from test_long_calls import LongCallBase
from test_long_tasks import long_task
from test_mvp import cx

import agents  # noqa: E402
from agents import AdapterError  # noqa: E402

TASK = long_task(38_000)  # a realistic long research specification (Hebrew, English, Markdown, code, URLs)


def criteria(n, prefix="Requirement"):
    return [f"{prefix} {i}: the report documents finding {i} with a dated primary source, its evidence strength and limits."
            for i in range(1, n + 1)]


def plan(crit):
    return cx({"plan": "Iterative, skeptical research with independent verification.", "acceptance_criteria": crit,
               "instruction": "Build the research ledger first, then audit the leading systems against primary sources.",
               "user_summary_he": "תכננתי את המחקר."})


class TestCriteriaValidation(unittest.TestCase):
    def check(self, crit):
        return agents.validate_plan({"plan": "p", "acceptance_criteria": crit, "instruction": "do the first step"}, 4000)[0]

    def test_the_failed_runs_51_detailed_criteria_are_now_accepted_and_the_limit_is_explicit(self):
        self.assertEqual(len(self.check(criteria(51))["acceptance_criteria"]), 51)
        self.assertEqual(len(self.check(criteria(agents.MAX_CRITERIA))["acceptance_criteria"]), agents.MAX_CRITERIA)
        with self.assertRaises(AdapterError) as e:
            self.check(criteria(agents.MAX_CRITERIA + 1))
        self.assertIn(f"{agents.MAX_CRITERIA + 1} items; the maximum is {agents.MAX_CRITERIA}", e.exception.message)
        self.assertIn("without dropping any requirement", e.exception.message)

    def test_empty_placeholder_duplicate_and_wrongly_typed_criteria_are_rejected_precisely(self):
        good = criteria(3)
        cases = {
            "it must be a JSON array of strings": "all of it",
            "at least one acceptance criterion is required": [],
            "item 2 is not a string": [good[0], {"text": good[1]}],
            "item 2 is empty or not a meaningful": [good[0], "   "],
            "item 3 is empty or not a meaningful": good[:2] + ["TBD"],
            "item 1 is empty or not a meaningful": ["ok done"],
            "item 3 duplicates item 1": good[:2] + [good[0].upper() + "  "],
            "item 2 has 1001 characters": [good[0], "x " * 500 + "y"],
        }
        for expected, crit in cases.items():
            with self.subTest(expected):
                with self.assertRaises(AdapterError) as e:
                    self.check(crit)
                self.assertIn(expected, e.exception.message)
        self.assertEqual(len(self.check(["Section A states the verdict and its confidence.",
                                         "Section B states the verdict and its confidence."])["acceptance_criteria"]), 2,
                         "criteria that differ by one letter are distinct")

    def test_the_reviewer_status_for_many_criteria_is_kept(self):
        rv, _, _ = agents.validate_review({"verdict": "CONTINUE", "findings": [], "next_instruction": "Audit Zep's temporal graph next.",
                                           "question": "", "criteria_status": ["unverified"] * 51}, 4000)
        self.assertEqual(len(rv["criteria_status"]), 51)


class TestBoundedPlanCorrection(LongCallBase):
    def test_an_over_limit_plan_is_corrected_and_the_research_proceeds(self):
        o, st = self.run_goal([plan(criteria(81)), plan(criteria(60)), OK_REVIEW], [WRITE_GOOD], task=TASK)
        self.assertEqual(st["status"], "COMPLETE", st["stop_reason"])
        self.assertEqual(len(st["acceptance_criteria"]), 60)
        self.assertEqual(st["calls"], {"codex": 3, "claude": 1}, "the correction is a real call, counted in the budget")
        first, second = [c["kw"]["stdin_text"] for c in self.fr.exec_calls()[:2]]
        self.assertNotIn("CORRECTION REQUIRED", first)
        self.assertIn("CORRECTION REQUIRED", second)
        self.assertIn("81 items; the maximum is 80", second, "the exact validation error is given back")
        self.assertIn(TASK, second, "the full original task is still given")
        self.assertIn("<previous_response>", second)
        self.assertIn("Requirement 81:", second)
        ev = [json.loads(x) for x in (o.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        corr = [e["data"] for e in ev if e["event"] == "plan_correction_requested"]
        self.assertEqual([(c["attempt"], c["of"]) for c in corr], [(1, 2)])
        self.assertEqual(json.loads((o.run_dir / "state.json").read_text(encoding="utf-8"))["task"], TASK)

    def test_correction_is_bounded_and_never_fabricates_criteria(self):
        bad = [plan(["", "TBD"]), plan(criteria(2) + ["n/a"]), cx({"plan": "p", "acceptance_criteria": "see task", "instruction": "x",
                                                                     "user_summary_he": ""})]
        o, st = self.run_goal(bad, [WRITE_GOOD], task=TASK)
        self.assertEqual(st["status"], "FAILED")
        self.assertTrue(st["stop_reason"].startswith("malformed_output: field 'acceptance_criteria' invalid: it must be a JSON array"),
                        st["stop_reason"])
        self.assertIn("(after 2 correction requests)", st["stop_reason"])
        self.assertEqual(st["calls"], {"codex": 3, "claude": 0}, "1 answer + 2 corrections, then stop: no endless retries")
        self.assertEqual(st["acceptance_criteria"], [], "nothing is invented")
        self.assertEqual(self.claude_calls(), [])

    def test_errors_that_are_not_format_errors_are_not_retried(self):
        from agents import AdapterError as AE
        o, st = self.run_goal([AE("auth", "not logged in")], [], task=TASK)
        self.assertEqual((st["status"], st["calls"]["codex"]), ("BLOCKED", 1))

    def test_corrections_respect_the_owners_codex_budget(self):
        o, st = self.run_goal([plan([]), plan([]), plan([])], [], budget={"max_codex_calls": 2}, task=TASK)
        self.assertEqual(st["status"], "PAUSED")
        self.assertEqual(st["stop_reason"], "budget_reached: codex calls")
        self.assertEqual(st["calls"]["codex"], 2)


if __name__ == "__main__":
    unittest.main()
