"""Per-interface time-series telemetry store.

REAL BACKEND this interface maps to: gNMI streaming telemetry subscriptions
on the emulated devices (interface utilisation, error counters, latency
probes), buffered by a collector. The demo keeps a bounded in-memory ring
buffer per interface, sampled once per virtual tick.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque


class TelemetrySource(ABC):
    """Abstract read API used by the heartbeat and the diagnostic tools."""

    @abstractmethod
    def series(self, interface: str, field: str, window: int) -> list[tuple[int, float]]:
        """Last `window` samples of a field as (tick, value) pairs."""

    @abstractmethod
    def interfaces(self) -> list[str]: ...


class TelemetryStore(TelemetrySource):
    """Ring-buffer implementation, deep-copyable for twin snapshots."""

    FIELDS = ("utilisation_pct", "errors", "latency_ms")

    def __init__(self, maxlen: int = 64) -> None:
        self.maxlen = maxlen
        # {interface: {field: deque[(tick, value)]}}
        self._buf: dict[str, dict[str, deque]] = {}

    def append(self, interface: str, tick: int, **fields: float) -> None:
        per_if = self._buf.setdefault(
            interface, {f: deque(maxlen=self.maxlen) for f in self.FIELDS}
        )
        for field, value in fields.items():
            per_if[field].append((tick, float(value)))

    def series(self, interface: str, field: str, window: int) -> list[tuple[int, float]]:
        per_if = self._buf.get(interface)
        if per_if is None or field not in per_if:
            return []
        buf = per_if[field]
        return list(buf)[-window:]

    def interfaces(self) -> list[str]:
        return sorted(self._buf)
