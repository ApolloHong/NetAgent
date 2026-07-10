"""NUAR adapter: fixture load, wrap/gap handling, replay clock stepping."""

from __future__ import annotations

import unittest
from pathlib import Path

from demo.inventory.identity import IdentityMap
from demo.twin.nuar_telemetry import NuarTelemetrySource, load_nuar_export

FIXTURES = Path(__file__).resolve().parent.parent / "demo" / "fixtures"


class TestNuarTelemetry(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.identity = IdentityMap.from_file(FIXTURES / "inventory.json")
        cls.export = load_nuar_export(FIXTURES / "nuar" / "nuar_export.json")
        cls.source = NuarTelemetrySource(cls.export, cls.identity)

    def test_canonical_interfaces(self) -> None:
        names = self.source.interfaces()
        self.assertIn("core3:ge-0/0/1", names)  # normalised, not NUAR ids
        self.assertTrue(all(":" in n for n in names))

    def test_counter_reset_dropped(self) -> None:
        # core1:ge-0/0/0 has a counter reset at bucket 20: that boundary must
        # be dropped, and no negative/absurd rate may appear.
        clock = self.source.make_clock()
        while not clock.exhausted:
            clock.tick()
        series = self.source.series("core1:ge-0/0/0", "utilisation_pct", 999)
        values = [v for _, v in series]
        self.assertEqual(len(series), 70)  # 71 boundaries - 1 reset
        self.assertTrue(all(0.0 <= v <= 100.0 for v in values))
        self.assertGreater(min(values), 20.0)  # no bogus near-zero rate

    def test_gap_dropped(self) -> None:
        # core2:ge-0/0/0 misses buckets 30-32: rates across the hole must not
        # be fabricated (3 missing buckets + the >gap boundary after them).
        clock = self.source.make_clock()
        while not clock.exhausted:
            clock.tick()
        series = self.source.series("core2:ge-0/0/0", "utilisation_pct", 999)
        self.assertEqual(len(series), 67)

    def test_replay_clock_gates_visibility(self) -> None:
        clock = self.source.make_clock()
        self.assertEqual(self.source.series("core3:ge-0/0/1", "utilisation_pct", 999), [])
        for _ in range(10):
            clock.tick()
        early = self.source.series("core3:ge-0/0/1", "utilisation_pct", 999)
        self.assertLessEqual(len(early), 10)
        self.assertTrue(all(t <= clock.now() for t, _ in early))
        self.assertEqual(round(clock.seconds_per_tick), 300)
        self.assertTrue(clock.time_str().startswith("2026-06-01"))

    def test_anomaly_visible_and_latency_absent(self) -> None:
        clock = self.source.make_clock()
        while not clock.exhausted:
            clock.tick()
        util = self.source.series("core3:ge-0/0/1", "utilisation_pct", 24)
        self.assertGreaterEqual(util[-1][1], 85.0)  # the recorded congestion
        self.assertEqual(self.source.series("core3:ge-0/0/1", "latency_ms", 24), [])


if __name__ == "__main__":
    unittest.main()
