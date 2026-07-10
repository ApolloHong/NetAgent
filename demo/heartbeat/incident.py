"""Incident and Symptom records emitted by the heartbeat.

An Incident is the handoff object between detection and RCA. Its `source`
field is the seam for future triggers: today only "heartbeat" emits
incidents; a capacity/traffic PREDICTOR will later emit source="forecast"
incidents (e.g. "corridor X will congest within 2h") and the RCA agent is
source-agnostic, so nothing downstream changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Symptom:
    kind: str  # congestion | link_down | session_down | session_flap |
    #            errors | latency | traffic_drop | config_drift
    object: str  # link id, session id, or "node:path" for config drift
    detail_fr: str
    tick: int  # when the heartbeat saw it
    onset_tick: int  # estimated onset (change-point or event time)
    value: float | None = None


@dataclass
class Incident:
    id: str
    first_seen_tick: int
    symptoms: list[Symptom] = field(default_factory=list)
    provisional_scope: set[str] = field(default_factory=set)
    source: str = "heartbeat"  # future: "forecast" (capacity predictor seam)
    # Accumulated ground-truth symptom signature (sets per SIGNATURE_KEYS),
    # merged each patrol while the incident is open. This is what the
    # counterfactual replay must reproduce.
    signature: dict[str, set] = field(default_factory=dict)

    def merge_signature(self, sig: dict[str, set]) -> None:
        for key, values in sig.items():
            self.signature.setdefault(key, set()).update(values)

    def summary_fr(self) -> str:
        return " ; ".join(s.detail_fr for s in self.symptoms)
