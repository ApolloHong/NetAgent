"""EVE-NG twin adapter: fixture-backed integration tests (fully offline)."""

from __future__ import annotations

import unittest
from pathlib import Path

from demo.inventory.identity import IdentityMap
from demo.twin.eve_twin import (
    EveNgTwin,
    EveWriteDisabled,
    FixtureEveTransport,
    FixtureMcpTransport,
    parse_commit_history,
    set_lines_to_paths,
)

FIXTURES = Path(__file__).resolve().parent.parent / "demo" / "fixtures"


def make_twin(allow_writes: bool = False) -> EveNgTwin:
    return EveNgTwin(
        eve=FixtureEveTransport(FIXTURES / "eve"),
        mcp=FixtureMcpTransport(FIXTURES / "eve" / "mcp_responses.json"),
        identity=IdentityMap.from_file(FIXTURES / "inventory.json"),
        golden_dir=FIXTURES / "eve" / "golden",
        allow_writes=allow_writes,
    )


class TestSetLineParsing(unittest.TestCase):
    def test_normalises_to_sim_compatible_paths(self) -> None:
        paths = set_lines_to_paths(
            "set protocols isis interface ge-0/0/0.0 metric 1000\n"
            "set interfaces ge-0/0/0 mtu 9192\n"
            "set protocols bgp group CUSTOMERS neighbor ce-C export EXPORT-CUST\n"
        )
        self.assertEqual(paths["protocols.isis.interface.ge-0/0/0.metric"], "1000")
        self.assertEqual(paths["interfaces.ge-0/0/0.mtu"], "9192")
        self.assertEqual(
            paths["protocols.bgp.group.CUSTOMERS.neighbor.ce-C.export"], "EXPORT-CUST"
        )

    def test_commit_history_parsing(self) -> None:
        commits = parse_commit_history(
            "0   2026-07-08 10:31:35 UTC by netops via netconf\n"
            "1   2026-07-01 09:00:00 UTC by admin via cli\n"
        )
        self.assertEqual(commits[0]["timestamp"], "2026-07-08 10:31:35")
        self.assertEqual(commits[0]["user"], "netops")


class TestEveTwinReadOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.twin = make_twin()

    def test_topology_normalised_to_canonical_ids(self) -> None:
        self.assertEqual(len(self.twin.get_nodes()), 8)
        links = self.twin.get_links()
        self.assertEqual(len(links), 8)
        self.assertIn("core1-core2", [l["id"] for l in links])
        self.assertTrue(all(l["oper_up"] for l in links))
        self.assertTrue(all(l["offered_mbps"] is None for l in links))  # no faked flows

    def test_config_drift_with_real_commit_timestamp(self) -> None:
        diff = self.twin.config.diff("core1")
        self.assertEqual(len(diff), 1)
        entry = diff[0]
        self.assertEqual(entry["path"], "protocols.isis.interface.ge-0/0/0.metric")
        self.assertEqual((entry["golden"], entry["running"]), ("10", "1000"))
        self.assertEqual(entry["changed_at_commit"], "2026-07-08 10:31:35")
        self.assertIsNotNone(entry["changed_at_tick"])
        self.assertEqual(self.twin.config.diff("core2"), [])

    def test_control_plane_state(self) -> None:
        bgp = self.twin.get_bgp_sessions()
        self.assertEqual({s["id"] for s in bgp},
                         {"edge1~ce-A", "edge2~ce-B", "edge3~ce-C", "edge4~ce-D"})
        self.assertTrue(all(s["state"] == "Established" for s in bgp))
        isis = self.twin.get_isis_adjacencies()
        self.assertTrue(all(a["state"] == "Up" for a in isis))

    def test_reachability_from_rib_without_flows(self) -> None:
        flows = self.twin.get_flows()
        self.assertEqual(len(flows), 4)
        for flow in flows:
            self.assertEqual(flow["status"], "ok")
            self.assertIsNone(flow["mbps"])  # traffic-matrix gap, documented
            self.assertIsNone(flow["path"])
            self.assertEqual(len(flow["reachable_from"]), 3)

    def test_traffic_matrix_seam_is_explicit(self) -> None:
        with self.assertRaises(NotImplementedError):
            self.twin.traffic_matrix.demands()

    def test_writes_are_gated(self) -> None:
        with self.assertRaises(EveWriteDisabled):
            self.twin.commit_config("core1", "set interfaces ge-0/0/0 mtu 8000")
        with self.assertRaises(EveWriteDisabled):
            self.twin.rollback_to_golden("core1")
        with self.assertRaises(EveWriteDisabled):
            self.twin.config.set("core1", "interfaces.ge-0/0/0.mtu", 8000, 1)


class TestEveTwinPhase2Writes(unittest.TestCase):
    """Phase 2 on the FIXTURE transport: commits recorded in-memory so the
    write code path is exercised offline (a real lab behaves identically)."""

    def test_inject_then_rollback_to_golden(self) -> None:
        twin = make_twin(allow_writes=True)
        # inject: MTU drift on core1 (a config-drift fault as a real commit)
        twin.commit_config("core1", "set interfaces ge-0/0/1 mtu 8000")
        drift_paths = {e["path"] for e in twin.config.diff("core1")}
        self.assertIn("interfaces.ge-0/0/1.mtu", drift_paths)
        # recover: declarative rollback to golden
        twin.rollback_to_golden("core1")
        self.assertEqual(twin.config.diff("core1"), [])  # metric AND mtu reverted


if __name__ == "__main__":
    unittest.main()
