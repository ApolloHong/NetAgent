"""Telemetry replay lane: builtin engine over the NUAR fixture, gNMI parse
helper, and the builtin-vs-Nautilus parity check (skipped cleanly when the
optional nautilus_trader extra is not installed)."""

from __future__ import annotations

import unittest
from pathlib import Path

from demo.engine.base import BuiltinReplayEngine
from demo.engine.nautilus_engine import NAUTILUS_AVAILABLE, parse_gnmi_update
from demo.inventory.identity import IdentityMap
from demo.twin.nuar_telemetry import NuarTelemetrySource, load_nuar_export

FIXTURES = Path(__file__).resolve().parent.parent / "demo" / "fixtures"


def make_source() -> tuple[NuarTelemetrySource, IdentityMap]:
    identity = IdentityMap.from_file(FIXTURES / "inventory.json")
    export = load_nuar_export(FIXTURES / "nuar" / "nuar_export.json")
    return NuarTelemetrySource(export, identity), identity


def summarize(incidents) -> list[tuple]:
    return [
        (s.kind, s.object, s.onset_tick, s.detail_fr)
        for incident in incidents
        for s in incident.symptoms
    ]


class TestBuiltinReplayEngine(unittest.TestCase):
    def test_detects_recorded_congestion_without_false_positives(self) -> None:
        source, identity = make_source()
        incidents = BuiltinReplayEngine(source, identity).run()
        self.assertEqual(len(incidents), 1)  # exactly the recorded anomaly
        kinds = {(s.kind, s.object) for s in incidents[0].symptoms}
        self.assertIn(("congestion", "core3-core4"), kinds)
        # the diurnal ramp, the counter reset and the gap must NOT alarm
        self.assertTrue(all(obj == "core3-core4" for _, obj in kinds))
        # detection happens when the recorded step crosses the threshold
        self.assertGreaterEqual(incidents[0].first_seen_tick, 45)

    def test_replay_incidents_use_real_timestamps(self) -> None:
        source, identity = make_source()
        clock = source.make_clock()
        self.assertTrue(clock.time_str(50).startswith("2026-06-01"))


@unittest.skipUnless(NAUTILUS_AVAILABLE, "optional extra: pip install .[nautilus]")
class TestNautilusParity(unittest.TestCase):
    def test_same_incidents_as_builtin(self) -> None:
        from demo.engine.nautilus_engine import NautilusReplayEngine

        source, identity = make_source()
        builtin = BuiltinReplayEngine(source, identity).run()
        source2, identity2 = make_source()  # fresh clock/state
        nautilus = NautilusReplayEngine(source2, identity2).run()
        self.assertEqual(summarize(builtin), summarize(nautilus))
        self.assertEqual(
            [i.first_seen_tick for i in builtin],
            [i.first_seen_tick for i in nautilus],
        )


class TestGnmiParsing(unittest.TestCase):
    def test_parse_gnmi_update(self) -> None:
        update = {
            "update": {
                "timestamp": 1_780_000_000_000_000_000,
                "prefix": "interfaces/interface[name=ge-0/0/0]",
                "update": [
                    {"path": "state/counters/in-octets", "val": 123456},
                    {"path": "state/counters/out-octets", "val": 654321},
                ],
            }
        }
        rows = parse_gnmi_update(update)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ifname"], "ge-0/0/0")
        self.assertEqual(rows[0]["value"], 123456)
        self.assertTrue(rows[0]["path"].endswith("in-octets"))


if __name__ == "__main__":
    unittest.main()
