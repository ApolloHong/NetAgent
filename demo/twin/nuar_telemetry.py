"""NUAR historical telemetry adapter (TelemetrySource over real past data).

REAL BACKEND: NUAR, the operator's internal network inventory & historical
telemetry warehouse. There is NO public API, so this adapter consumes a
documented EXPORT SCHEMA (JSON) that a `--record` run (or an internal batch
job) produces; when the internal API shape is known, ONLY `load_nuar_export`
changes — everything downstream already speaks canonical interface ids.

Export schema (one counter row per interface per bucket):
    {
      "source": "NUAR export",
      "granularity_seconds": 300,
      "interfaces": [{"nuar_id": "NUAR:IF:100001", "speed_mbps": 1000.0}],
      "samples": [
        {"nuar_id": "NUAR:IF:100001", "ts": "2026-06-01T06:00:00Z",
         "in_octets": 123456789, "out_octets": 98765432, "in_errors": 0},
        ...
      ]
    }

Counter semantics handled here (real-world SNMP/streaming quirks):
  - RATES from cumulative octet counters (utilisation % of interface speed);
  - counter RESETS / WRAPS: a negative delta invalidates that boundary — the
    sample is dropped, never emitted as a bogus rate;
  - GAPS: a bucket spacing > 1.5x the export granularity is a hole — no rate
    is fabricated across it;
  - COARSE granularity: NUAR is 5-minute historical truth. It is the right
    source for validating detection thresholds and false-positive behaviour
    against real daily/weekly patterns, and as future predictor training
    data (feeding the existing `Incident.source="forecast"` seam). It is NOT
    a source of second-scale on-change signals — those come from gNMI on the
    twin (see demo/engine/nautilus_engine.py GnmiTelemetryFeed).
  - NO latency series: NUAR exports carry no latency probes; `series(...,
    "latency_ms")` returns [] and the shared checks skip empty series.

Detection thresholds and change-point parameters WILL need retuning on real
data — they are injectable (demo/heartbeat/checks.py Thresholds, overridable
from config.yaml), not hardcoded.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from demo.clock import ReplayClock
from demo.inventory.identity import IdentityMap
from demo.twin.telemetry import TelemetrySource

GAP_FACTOR = 1.5  # bucket spacing beyond granularity*GAP_FACTOR is a hole


def _parse_ts(value: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_nuar_export(path: str | Path) -> dict[str, Any]:
    """Load a NUAR export file. The single place that changes when the real
    NUAR API shape becomes available internally."""
    return json.loads(Path(path).read_text())


class NuarTelemetrySource(TelemetrySource):
    """Real historical per-interface counters, normalised to canonical ids."""

    def __init__(self, export: dict[str, Any], identity: IdentityMap) -> None:
        self.identity = identity
        self.granularity = int(export.get("granularity_seconds", 300))
        speeds = {
            row["nuar_id"]: float(row.get("speed_mbps") or 0.0)
            for row in export.get("interfaces", [])
        }

        per_nuar: dict[str, list[dict]] = {}
        for sample in export.get("samples", []):
            per_nuar.setdefault(sample["nuar_id"], []).append(sample)

        # Derive per-boundary rates, dropping resets/wraps and gaps.
        rates: dict[str, dict[_dt.datetime, tuple[float, float]]] = {}
        all_ts: set[_dt.datetime] = set()
        for nuar_id, samples in per_nuar.items():
            canonical = identity.iface_for_nuar(nuar_id)  # raises if unmapped
            speed = speeds.get(nuar_id) or identity.capacity_for_iface(canonical) or 0.0
            samples.sort(key=lambda s: s["ts"])
            series: dict[_dt.datetime, tuple[float, float]] = {}
            for prev, cur in zip(samples, samples[1:]):
                t1, t2 = _parse_ts(prev["ts"]), _parse_ts(cur["ts"])
                dt = (t2 - t1).total_seconds()
                if dt <= 0 or dt > self.granularity * GAP_FACTOR:
                    continue  # duplicate timestamp or gap: no fabricated rate
                d_in = cur["in_octets"] - prev["in_octets"]
                d_out = cur["out_octets"] - prev["out_octets"]
                d_err = cur.get("in_errors", 0) - prev.get("in_errors", 0)
                if d_in < 0 or d_out < 0:
                    continue  # counter reset/wrap: drop this boundary
                if speed <= 0:
                    continue  # unknown speed: cannot express utilisation
                bits = max(d_in, d_out) * 8.0
                util = min(max(bits / (dt * speed * 1e6) * 100.0, 0.0), 100.0)
                errors = float(d_err) if d_err >= 0 else 0.0
                series[t2] = (util, errors)
                all_ts.add(t2)
            rates[canonical] = series

        # Global replay timeline: sorted union of emitted bucket timestamps.
        self.timeline: list[_dt.datetime] = sorted(all_ts)
        tick_of = {ts: i + 1 for i, ts in enumerate(self.timeline)}
        # Per-interface aligned series: (tick, value) pairs on that timeline.
        self._util: dict[str, list[tuple[int, float]]] = {}
        self._errors: dict[str, list[tuple[int, float]]] = {}
        for canonical, series in rates.items():
            pairs = sorted((tick_of[ts], values) for ts, values in series.items())
            self._util[canonical] = [(t, v[0]) for t, v in pairs]
            self._errors[canonical] = [(t, v[1]) for t, v in pairs]

        self._clock: ReplayClock | None = None

    # ------------------------------------------------------------------
    def make_clock(self) -> ReplayClock:
        clock = ReplayClock(self.timeline)
        self._clock = clock
        return clock

    def bind_clock(self, clock: ReplayClock) -> None:
        self._clock = clock

    # ---- TelemetrySource ----------------------------------------------
    def series(self, interface: str, field: str, window: int) -> list[tuple[int, float]]:
        store = {
            "utilisation_pct": self._util,
            "errors": self._errors,
            "latency_ms": None,  # not available from NUAR (documented above)
        }.get(field)
        if store is None:
            return []
        samples = store.get(interface, [])
        horizon = self._clock.now() if self._clock is not None else float("inf")
        visible = [s for s in samples if s[0] <= horizon]
        return visible[-window:]

    def interfaces(self) -> list[str]:
        return sorted(self._util)
