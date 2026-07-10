"""Telemetry replay/live lane: drive a TelemetrySource through a clock into
the SHARED heartbeat detection checks and emit Incidents.

This lane is telemetry-only (no config audit, no twin ground truth): it is
how detection thresholds and false-positive behaviour get validated against
REAL historical traffic (NUAR buckets) — and, with the optional Nautilus
engine + gNMI feed, how the identical detection logic runs live.

The detection LOGIC itself lives in demo/heartbeat/checks.py and is shared —
never duplicated — between:
  - the sim heartbeat detector,
  - BuiltinReplayEngine (this file, the default per-tick loop),
  - the optional NautilusTrader HeartbeatActor (nautilus_engine.py).
Both engines also share `LaneAggregator`, so a parity test can require the
SAME incidents from both on the same fixture.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Protocol

from demo.heartbeat.checks import Thresholds, telemetry_checks
from demo.heartbeat.incident import Incident, Symptom
from demo.inventory.identity import IdentityMap

ATTACH_WINDOW = 8  # ticks: same incident-attachment semantics as the detector


class ReplaySource(Protocol):
    """A TelemetrySource that also carries a replayable timeline
    (NuarTelemetrySource satisfies this protocol)."""

    def series(self, interface: str, field: str, window: int) -> list[tuple[int, float]]: ...
    def interfaces(self) -> list[str]: ...
    def make_clock(self): ...  # -> ReplayClock over the recording's timestamps


class LaneAggregator:
    """Symptom -> Incident aggregation for the telemetry lane (pure).

    Same semantics as the heartbeat detector: global (kind, object) dedup,
    new symptoms within ATTACH_WINDOW ticks join the open incident. No twin
    signature here — this lane has telemetry only.
    """

    def __init__(self, time_str: Callable[[int], str], verbose: bool = False) -> None:
        self._time_str = time_str
        self._verbose = verbose
        self._seen: set[tuple[str, str]] = set()
        self._counter = 0
        self.incidents: list[Incident] = []
        self.open_incident: Incident | None = None

    def observe(self, tick: int, symptoms: list[Symptom]) -> Incident | None:
        fresh = [s for s in symptoms if (s.kind, s.object) not in self._seen]
        for s in fresh:
            self._seen.add((s.kind, s.object))
        if not fresh:
            return None
        if (
            self.open_incident is not None
            and tick - self.open_incident.first_seen_tick <= ATTACH_WINDOW
        ):
            self.open_incident.symptoms.extend(fresh)
            self.open_incident.provisional_scope.update(s.object for s in fresh)
            if self._verbose:
                for s in fresh:
                    self._say(tick, f"symptome supplementaire rattache a "
                                    f"{self.open_incident.id}: {s.detail_fr}")
            return None
        self._counter += 1
        incident = Incident(
            id=f"INC-{self._counter:04d}",
            first_seen_tick=tick,
            symptoms=list(fresh),
            provisional_scope={s.object for s in fresh},
        )
        self.incidents.append(incident)
        self.open_incident = incident
        if self._verbose:
            self._say(tick, f"anomalie: {incident.summary_fr()}. "
                            f"Incident {incident.id} cree.")
        return incident

    def _say(self, tick: int, msg: str) -> None:
        print(f"[HEARTBEAT] {self._time_str(tick)} {msg}")


def evaluate_tick(
    source,
    identity: IdentityMap,
    tick: int,
    time_str: Callable[[int], str],
    th: Thresholds,
) -> list[Symptom]:
    """One detection pass over every interface of a source (shared checks,
    deduped to one symptom per (kind, link) exactly like the detector)."""
    out: list[Symptom] = []
    per_link_done: set[tuple[str, str]] = set()
    for iface in source.interfaces():
        link = identity.link_for_iface(iface)
        drafts = telemetry_checks(
            link=link,
            utilisation=source.series(iface, "utilisation_pct", th.telemetry_window),
            errors=source.series(iface, "errors", th.telemetry_window),
            latency=source.series(iface, "latency_ms", th.telemetry_window),
            tick=tick,
            time_str=time_str,
            th=th,
        )
        for symptom in drafts:
            key = (symptom.kind, link)
            if key not in per_link_done:
                per_link_done.add(key)
                out.append(symptom)
    return out


class TelemetryReplayEngine(ABC):
    """One engine for the telemetry replay/live lane."""

    name = "engine"

    @abstractmethod
    def run(self) -> list[Incident]:
        """Replay the recording through the detection checks; return the
        emitted Incidents (source='heartbeat', telemetry-only)."""


class BuiltinReplayEngine(TelemetryReplayEngine):
    """Default engine: the plain per-tick loop (no extra dependency)."""

    name = "builtin"

    def __init__(
        self,
        source: ReplaySource,
        identity: IdentityMap,
        thresholds: Thresholds | None = None,
        verbose: bool = False,
    ) -> None:
        self._source = source
        self._identity = identity
        self._th = thresholds or Thresholds()
        self._verbose = verbose

    def run(self) -> list[Incident]:
        clock = self._source.make_clock()
        aggregator = LaneAggregator(time_str=clock.time_str, verbose=self._verbose)
        while not clock.exhausted:
            tick = clock.tick()
            symptoms = evaluate_tick(
                self._source, self._identity, tick, clock.time_str, self._th
            )
            aggregator.observe(tick, symptoms)
        return aggregator.incidents
