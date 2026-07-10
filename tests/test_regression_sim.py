"""Regression: the ALL-SIM defaults must behave exactly as before the
real-backend work — byte-for-byte scenario traces and a 7/7 eval.

Run from the repo root: python -m unittest discover tests
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden"
SCENARIOS = ["config_drift", "link_down_with_congestion", "bgp_session_flap"]


class TestScenarioGoldenTraces(unittest.TestCase):
    """Byte-for-byte comparison against traces captured before the change."""

    def test_traces_match_golden(self) -> None:
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                proc = subprocess.run(
                    [sys.executable, "-m", "demo", "run", "--scenario", scenario],
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                golden = (GOLDEN / f"{scenario}.txt").read_text()
                self.assertEqual(proc.stdout, golden, f"trace drifted for {scenario}")


class TestEvalStillPerfect(unittest.TestCase):
    """The default eval must stay 7/7 detection and 7/7 diagnosis."""

    def test_eval_seven_of_seven(self) -> None:
        from demo.eval.runner import run_eval

        with contextlib.redirect_stdout(io.StringIO()):
            results = run_eval(reasoner="rules", seed=2026)

        fault_cases = [r for r in results if r.expected_detection]
        clean_cases = [r for r in results if not r.expected_detection]
        self.assertEqual(len(fault_cases), 7)
        self.assertEqual(sum(1 for r in fault_cases if r.detected), 7, "detection != 7/7")
        self.assertEqual(
            sum(1 for r in fault_cases if r.diagnosis_pass), 7, "diagnosis != 7/7"
        )
        self.assertEqual(
            sum(1 for r in fault_cases if r.clients_pass), 7, "affected clients != 7/7"
        )
        self.assertEqual(
            sum(1 for r in fault_cases if r.recover_ok and r.clean_ok), 7, "recovery != 7/7"
        )
        self.assertEqual(sum(r.false_positives for r in results), 0, "false positives")
        self.assertTrue(all(not r.error for r in results), "errors during eval")
        self.assertTrue(all(not c.detected for c in clean_cases))


if __name__ == "__main__":
    unittest.main()
