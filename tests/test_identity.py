"""Identity mapping: bijectivity, round-trips, duplicate rejection."""

from __future__ import annotations

import unittest
from pathlib import Path

from demo.inventory.identity import (
    DeviceRecord,
    IdentityError,
    IdentityMap,
    InterfaceRecord,
    link_id,
)

FIXTURE = Path(__file__).resolve().parent.parent / "demo" / "fixtures" / "inventory.json"


class TestIdentityMap(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = IdentityMap.from_file(FIXTURE)

    def test_round_trips_are_bijective(self) -> None:
        for record in self.identity.interfaces:
            self.assertEqual(
                self.identity.iface_for_nuar(self.identity.nuar_for_iface(record.canonical)),
                record.canonical,
            )
        for record in self.identity.devices:
            self.assertEqual(
                self.identity.device_for_eve_node(record.eve_node_id), record.canonical
            )
            self.assertEqual(
                self.identity.device_for_eve_name(record.eve_name), record.canonical
            )
            self.assertEqual(
                self.identity.device_for_mcp(self.identity.mcp_for_device(record.canonical)),
                record.canonical,
            )

    def test_config_key_and_link(self) -> None:
        device, ifname = self.identity.config_key("core1:ge-0/0/0")
        self.assertEqual((device, ifname), ("core1", "ge-0/0/0"))
        self.assertEqual(self.identity.link_for_iface("core1:ge-0/0/0"), "core1-core2")
        self.assertEqual(len(self.identity.interfaces_of_link("core1-core2")), 2)
        self.assertEqual(link_id("core2", "core1"), "core1-core2")

    def test_matches_sim_twin(self) -> None:
        from demo.twin.sim_twin import build_default_twin

        twin = build_default_twin()
        derived = IdentityMap.from_sim_twin(twin)
        self.assertEqual(
            [d.canonical for d in derived.devices],
            [d.canonical for d in self.identity.devices],
        )
        self.assertEqual(derived.link_ids(), sorted(twin.links))
        self.assertEqual(derived.prefix_ids(), self.identity.prefix_ids())

    def test_duplicates_rejected(self) -> None:
        with self.assertRaises(IdentityError):
            IdentityMap(
                devices=[DeviceRecord("core1", eve_node_id="1"), DeviceRecord("core1")]
            )
        with self.assertRaises(IdentityError):
            IdentityMap(
                interfaces=[
                    InterfaceRecord("a:ge-0/0/0", "a", "ge-0/0/0", "a-b", nuar_id="X"),
                    InterfaceRecord("b:ge-0/0/0", "b", "ge-0/0/0", "a-b", nuar_id="X"),
                ]
            )

    def test_unknown_ids_raise(self) -> None:
        with self.assertRaises(IdentityError):
            self.identity.iface_for_nuar("NUAR:IF:999999")
        with self.assertRaises(IdentityError):
            self.identity.device_for_eve_node("42")


if __name__ == "__main__":
    unittest.main()
