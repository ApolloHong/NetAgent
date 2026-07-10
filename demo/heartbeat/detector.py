"""Heartbeat detector: the periodic patrol that turns raw signals into
Incidents.

REAL BACKEND: a scheduled job consuming gNMI telemetry streams and syslog /
NETCONF event notifications, plus a periodic Batfish (or junos-mcp-server
`show | compare`) audit of running config against the golden reference.

SAFETY: this layer only DETECTS and REPORTS. It holds no mutation handle on
the twin — it reads telemetry, events, config diffs and the ground-truth
signature, and emits Incident objects. It never changes configuration.

Detection methods, deliberately simple and defensible:
  - on-change events (link down, session down): caught within one tick;
  - telemetry checks (static thresholds + mean-shift change-point) — these
    live as PURE functions in demo/heartbeat/checks.py, shared verbatim with
    the telemetry replay engines (builtin and NautilusTrader lanes);
  - a config audit (running vs golden) every `thresholds.audit_every` ticks:
    slow drifts are caught within a few ticks.

Thresholds are injectable (defaults identical to the historical constants)
because real 5-minute NUAR data needs different tuning than the sim.
"""

from __future__ import annotations

from demo.heartbeat.checks import Thresholds, telemetry_checks
from demo.heartbeat.incident import Incident, Symptom
from demo.twin.sim_twin import SimTwin

ATTACH_WINDOW = 8  # new symptoms within this window join the open incident


class HeartbeatDetector:
    """Periodic patrol over one twin. Read-only by construction."""

    def __init__(
        self, twin: SimTwin, verbose: bool = True, thresholds: Thresholds | None = None
    ) -> None:
        self.twin = twin
        self.verbose = verbose
        self.thresholds = thresholds or Thresholds()
        self._event_idx = len(twin.events)  # only react to future events
        self._seen: set[tuple[str, str]] = set()  # (kind, object) dedup
        self._counter = 0
        self.incidents: list[Incident] = []
        self.open_incident: Incident | None = None

    # ------------------------------------------------------------------
    def patrol(self) -> Incident | None:
        """Run one detection pass; returns a NEW incident if one was opened."""
        t = self.twin.clock.now()
        symptoms = self._collect_events(t)
        symptoms += self._collect_telemetry(t)
        if t % self.thresholds.audit_every == 0:
            symptoms += self._collect_config_audit(t)

        fresh = [s for s in symptoms if (s.kind, s.object) not in self._seen]
        for s in fresh:
            self._seen.add((s.kind, s.object))

        new_incident: Incident | None = None
        if fresh:
            if (
                self.open_incident is not None
                and t - self.open_incident.first_seen_tick <= ATTACH_WINDOW
            ):
                self.open_incident.symptoms.extend(fresh)
                self.open_incident.provisional_scope.update(s.object for s in fresh)
                if self.verbose:
                    for s in fresh:
                        self._say(
                            t,
                            f"symptome supplementaire rattache a "
                            f"{self.open_incident.id}: {s.detail_fr}",
                        )
            else:
                new_incident = self._open_incident(t, fresh)

        # Keep the incident's ground-truth signature up to date while open,
        # so intermittent states (e.g. a flapping session's down phases) are
        # captured for the counterfactual comparison.
        if self.open_incident is not None:
            self.open_incident.merge_signature(self.twin.signature())
        return new_incident

    def close(self) -> None:
        self.open_incident = None

    # ------------------------------------------------------------------
    def _open_incident(self, t: int, symptoms: list[Symptom]) -> Incident:
        self._counter += 1
        incident = Incident(
            id=f"INC-{self._counter:04d}",
            first_seen_tick=t,
            symptoms=list(symptoms),
            provisional_scope={s.object for s in symptoms},
        )
        incident.merge_signature(self.twin.signature())
        self.incidents.append(incident)
        self.open_incident = incident
        if self.verbose:
            self._say(t, f"anomalie: {incident.summary_fr()}. Incident {incident.id} cree.")
        return incident

    def _say(self, tick: int, msg: str) -> None:
        print(f"[HEARTBEAT] {self.twin.clock.time_str(tick)} {msg}")

    # ------------------------------------------------------------------
    def _collect_events(self, t: int) -> list[Symptom]:
        out: list[Symptom] = []
        new_events = self.twin.events[self._event_idx :]
        self._event_idx = len(self.twin.events)
        session_downs_seen: dict[str, int] = {}
        for e in self.twin.events:
            if e["kind"] == "session_down":
                session_downs_seen[e["object"]] = session_downs_seen.get(e["object"], 0) + 1
        for e in new_events:
            if e["kind"] == "link_down":
                out.append(
                    Symptom(
                        kind="link_down",
                        object=e["object"],
                        detail_fr=f"lien {e['object']} tombe (evenement link_down)",
                        tick=t,
                        onset_tick=e["tick"],
                    )
                )
            elif e["kind"] == "session_down":
                if session_downs_seen.get(e["object"], 0) >= 2:
                    out.append(
                        Symptom(
                            kind="session_flap",
                            object=e["object"],
                            detail_fr=f"session BGP {e['object']} instable (flapping)",
                            tick=t,
                            onset_tick=e["tick"],
                        )
                    )
                else:
                    out.append(
                        Symptom(
                            kind="session_down",
                            object=e["object"],
                            detail_fr=f"session BGP {e['object']} tombee",
                            tick=t,
                            onset_tick=e["tick"],
                        )
                    )
        return out

    def _collect_telemetry(self, t: int) -> list[Symptom]:
        # The actual checks are the SHARED pure functions in checks.py; this
        # method only feeds them series and dedupes the two interfaces of a
        # link down to one symptom per (kind, link).
        out: list[Symptom] = []
        th = self.thresholds
        per_link_done: set[tuple[str, str]] = set()
        for iface in self.twin.telemetry.interfaces():
            link = self.twin.link_of_interface(iface)
            if link is None:
                continue
            drafts = telemetry_checks(
                link=link,
                utilisation=self.twin.telemetry.series(
                    iface, "utilisation_pct", th.telemetry_window
                ),
                errors=self.twin.telemetry.series(iface, "errors", th.telemetry_window),
                latency=self.twin.telemetry.series(iface, "latency_ms", th.telemetry_window),
                tick=t,
                time_str=self.twin.clock.time_str,
                th=th,
            )
            for symptom in drafts:
                key = (symptom.kind, link)
                if key not in per_link_done:
                    per_link_done.add(key)
                    out.append(symptom)
        return out

    def _collect_config_audit(self, t: int) -> list[Symptom]:
        out: list[Symptom] = []
        for entry in self.twin.config_diff_all():
            obj = f"{entry['node']}:{entry['path']}"
            out.append(
                Symptom(
                    kind="config_drift",
                    object=obj,
                    detail_fr=(
                        f"derive de configuration sur {entry['node']} "
                        f"({entry['path']}): {entry['golden']} -> {entry['running']}"
                    ),
                    tick=t,
                    onset_tick=entry.get("changed_at_tick") or t,
                )
            )
        return out
