"""Counterfactual oracles: sim oracle identical to history, Batfish oracle
discriminates hypotheses from captured answers, EVE oracle stays gated."""

from __future__ import annotations

import unittest
from pathlib import Path

from demo.faults.catalog import build_fault
from demo.heartbeat.detector import HeartbeatDetector
from demo.heartbeat.incident import Incident
from demo.rca.counterfactual import (
    BatfishCounterfactual,
    EveCounterfactual,
    FixtureBatfishBackend,
    SimCounterfactual,
    hypothesis_key,
)
from demo.twin.sim_twin import build_default_twin

FIXTURES = Path(__file__).resolve().parent.parent / "demo" / "fixtures"

DRIFT = {
    "type": "config_drift",
    "object": "isis_metric",
    "where": "core1-core2",
    "params": {"node": "core1", "new_value": 1000},
}
LINKDOWN = {"type": "link_down", "object": "link", "where": "core2-core4", "params": {}}
EXPORT = {
    "type": "config_drift",
    "object": "export_policy",
    "where": "edge3",
    "params": {"ce": "ce-C"},
}
FLAP = {
    "type": "session_flap",
    "object": "bgp_session",
    "where": "edge2~ce-B",
    "params": {},
}


def drift_incident() -> tuple:
    """Real drift incident produced by the sim pipeline (twin left faulty)."""
    twin = build_default_twin()
    detector = HeartbeatDetector(twin, verbose=False)
    fault = build_fault(DRIFT)
    fault.inject(twin)
    incident = None
    for _ in range(12):
        twin.tick()
        incident = detector.patrol() or incident
        if incident:
            break
    assert incident is not None
    return twin, incident


class TestSimCounterfactual(unittest.TestCase):
    def test_confirms_true_cause_and_rejects_others(self) -> None:
        twin, incident = drift_incident()
        oracle = SimCounterfactual(twin, incident)
        confirmed = oracle.test(DRIFT)
        self.assertTrue(confirmed["reproduced"])
        self.assertEqual(set(confirmed), {"hypothesis", "reproduced",
                                          "sandbox_signature", "differences"})
        rejected = oracle.test(LINKDOWN)
        self.assertFalse(rejected["reproduced"])
        self.assertIn("down_links", rejected["differences"])


class TestBatfishCounterfactual(unittest.TestCase):
    """Runs fully offline from captured Batfish answers."""

    def setUp(self) -> None:
        self.backend = FixtureBatfishBackend(FIXTURES / "batfish" / "answers.json")
        # A congestion-flavoured drift incident: no routing-plane symptom sets.
        self.incident = Incident(id="INC-T", first_seen_tick=20)
        self.incident.merge_signature(
            {"down_links": set(), "unreachable_prefixes": set()}
        )

    def test_confirms_drift_via_route_shift(self) -> None:
        oracle = BatfishCounterfactual(self.backend, self.incident)
        result = oracle.test(DRIFT)
        self.assertTrue(result["reproduced"])
        self.assertEqual(result["coverage"], "routing-only")
        self.assertTrue(result["changed_routes"])

    def test_rejects_wrong_hypotheses(self) -> None:
        oracle = BatfishCounterfactual(self.backend, self.incident)
        linkdown = oracle.test(LINKDOWN)  # predicts a down link we did not see
        self.assertFalse(linkdown["reproduced"])
        export = oracle.test(EXPORT)  # predicts a withdrawal we did not see
        self.assertFalse(export["reproduced"])

    def test_dynamic_hypotheses_reported_unsupported(self) -> None:
        oracle = BatfishCounterfactual(self.backend, self.incident)
        result = oracle.test(FLAP)
        self.assertFalse(result["reproduced"])
        self.assertTrue(result["unsupported"])

    def test_hypothesis_key_matches_fault_id(self) -> None:
        self.assertEqual(hypothesis_key(DRIFT), build_fault(DRIFT).fault_id)
        self.assertEqual(hypothesis_key(LINKDOWN), build_fault(LINKDOWN).fault_id)


class TestEveCounterfactualGated(unittest.TestCase):
    def test_requires_writes(self) -> None:
        class FakeTwin:
            allow_writes = False

        with self.assertRaises(PermissionError):
            EveCounterfactual(FakeTwin(), allow=False).test(DRIFT)


if __name__ == "__main__":
    unittest.main()
