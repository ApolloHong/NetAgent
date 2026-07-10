"""Rule-based reasoner: a deterministic diagnostic playbook (the default).

It drives the same tool registry as the LLM reasoner, through the same
orchestrator, and produces the same structured conclusion — so the demo
always runs offline with no API key, and the two reasoners are directly
comparable in the eval harness.

The playbook encodes how a NOC engineer works an incident:
  1. pin the ONSET time from telemetry (change-point);
  2. sweep evidence across the heterogeneous sources: config drift on every
     device, control-plane state (BGP / IS-IS), events from the incident;
  3. localise on the topology (which link, which flows rerouted);
  4. rank candidate causes with the cause-vs-effect discipline (temporal
     alignment + only injectable fault types can be causes);
  5. validate the top hypothesis by COUNTERFACTUAL REPLAY in the sandbox;
  6. conclude with cause, evidence chain, affected clients, confidence.

Implemented as a Python generator: each `yield ToolCall(...)` returns the
tool's result, which keeps the playbook readable as a linear procedure.
"""

from __future__ import annotations

from typing import Any, Generator

from demo.heartbeat.incident import Incident
from demo.rca.agent import AgentState, Conclusion, Note, Reasoner, ToolCall
from demo.rca.analysis import change_point, rank_candidates

_TELEMETRY_KINDS = {
    "congestion": "utilisation_pct",
    "traffic_drop": "utilisation_pct",
    "errors": "errors",
    "latency": "latency_ms",
}


class RulesReasoner(Reasoner):
    name = "rules"

    def __init__(self) -> None:
        self._gen: Generator | None = None

    def next_action(self, state: AgentState):
        if self._gen is None:
            self._gen = self._playbook(state)
            return next(self._gen)
        return self._gen.send(state.last_result)

    # ------------------------------------------------------------------
    def _playbook(self, state: AgentState) -> Generator:
        incident = state.incident
        evidence: list[str] = []

        telemetry_symptoms = [
            s for s in incident.symptoms if s.kind in _TELEMETRY_KINDS
        ]
        congestion = [s for s in incident.symptoms if s.kind == "congestion"]
        latency_symptoms = [s for s in incident.symptoms if s.kind == "latency"]

        # ---- 1. Pin the onset time -----------------------------------
        onset = min(s.onset_tick for s in incident.symptoms)
        impaired_links: list[dict[str, Any]] = []
        for symptom in telemetry_symptoms[:2]:
            result = yield ToolCall(
                "read_telemetry", {"interface": symptom.object, "window": 24}
            )
            if "error" in result:
                continue
            series = result[_TELEMETRY_KINDS[symptom.kind]]
            cp = change_point(series)
            if cp and cp["score"] >= 6.0:
                onset = min(onset, cp["tick"])
                evidence.append(
                    f"telemetrie {symptom.object}: rupture de la serie "
                    f"'{_TELEMETRY_KINDS[symptom.kind]}' a t={cp['tick']} "
                    f"(decalage {cp['shift']:+.0f})"
                )
            if symptom.kind == "latency" and cp:
                impaired_links.append(
                    {
                        "link": symptom.object,
                        "delay_ms": max(cp["shift"], 20.0),
                        "loss_pct": 2.0,
                    }
                )
        evidence.insert(
            0,
            f"symptomes ({incident.id}): {incident.summary_fr()} — debut estime t={onset}",
        )

        # ---- 2. Evidence sweep: config drift on every device ---------
        config_diffs: list[dict] = []
        for node in state.nodes:
            result = yield ToolCall("diff_config", {"node": node})
            if "error" not in result:
                config_diffs.extend(result["diff"])
        for entry in config_diffs:
            evidence.append(
                f"derive de config sur {entry['node']}: {entry['path']} "
                f"{entry['golden']} -> {entry['running']} (commit t={entry['changed_at_tick']})"
            )
        if not config_diffs:
            evidence.append("audit de configuration: aucun ecart au golden")

        # ---- 2b. Control plane ----------------------------------------
        bgp = yield ToolCall("get_bgp_state", {})
        bgp_sessions = bgp.get("sessions", [])
        unstable = [
            s
            for s in bgp_sessions
            if s["state"] != "Established" or s["transitions_recent"] >= 2
        ]
        for s in unstable:
            evidence.append(
                f"plan de controle: session {s['id']} {s['state']}, "
                f"{s['transitions_recent']} transitions recentes"
            )
        isis = yield ToolCall("get_isis_adjacencies", {})
        down_adj = [a for a in isis.get("adjacencies", []) if a["state"] != "Up"]
        for a in down_adj:
            evidence.append(f"plan de controle: adjacence IS-IS {a['link']} Down")

        # ---- 3. Topological localisation ------------------------------
        events = [
            {"kind": s.kind, "object": s.object, "tick": s.onset_tick}
            for s in incident.symptoms
            if s.kind in ("link_down", "session_down", "session_flap")
        ]
        biggest_flow = None
        if congestion:
            traffic = yield ToolCall(
                "get_link_traffic", {"link": congestion[0].object}
            )
            if "error" not in traffic and traffic["flows"]:
                biggest_flow = traffic["flows"][0]
            node_a = congestion[0].object.split("-")[0]
            yield ToolCall("get_topology_neighbours", {"node": node_a})
        affected_result = yield ToolCall("get_affected_clients", {"scope": "all"})
        affected = affected_result.get("affected", [])

        # If a corridor congested AND something upstream changed (config or
        # topology event), show the reroute explicitly: live vs baseline path.
        if biggest_flow and (config_diffs or events):
            live = yield ToolCall(
                "shortest_path",
                {"src": biggest_flow["src"], "dst": biggest_flow["dst"], "state": "live"},
            )
            base = yield ToolCall(
                "shortest_path",
                {
                    "src": biggest_flow["src"],
                    "dst": biggest_flow["dst"],
                    "state": "baseline",
                },
            )
            if (
                "error" not in live
                and "error" not in base
                and live["path"] != base["path"]
            ):
                evidence.append(
                    f"le trafic {biggest_flow['src']}->{biggest_flow['dst']} s'est "
                    f"reporte: {' > '.join(base['path'] or [])} devient "
                    f"{' > '.join(live['path'] or [])}"
                )

        # ---- 4. Rank hypotheses (cause vs effect) ----------------------
        hypotheses, notes = rank_candidates(
            onset_tick=onset,
            config_diffs=config_diffs,
            events=events,
            bgp_sessions=bgp_sessions,
            impaired_links=impaired_links,
            link_of_iface=state.link_of_iface,
        )
        for note in notes:
            yield Note(note)
        if congestion:
            yield Note(
                f"le lien congestionne {congestion[0].object} est traite comme un "
                "EFFET (la congestion n'est pas un type de panne injectable); "
                "la cause doit etre en amont."
            )
        if hypotheses:
            ranking = " ; ".join(
                f"{i}) {h['detail']} [score {h['score']:.0f}]"
                for i, h in enumerate(hypotheses[:3], 1)
            )
            yield Note(f"hypotheses classees: {ranking}")

        # ---- 5. Counterfactual validation in the sandbox ---------------
        confirmed: dict[str, Any] | None = None
        counterfactual: dict[str, Any] | None = None
        for hyp in hypotheses[:3]:
            result = yield ToolCall(
                "counterfactual_inject",
                {"hypothesis": {k: hyp[k] for k in ("type", "object", "where", "params")}},
                note_fr=(
                    f"hypothese: {hyp['detail']}. Validation contrefactuelle "
                    "dans le jumeau (bac a sable)..."
                ),
            )
            counterfactual = result
            if "error" not in result and result.get("reproduced"):
                confirmed = hyp
                evidence.append(
                    "contrefactuel: la re-injection de cette cause dans le bac a "
                    "sable reproduit exactement la signature des symptomes"
                )
                break
            evidence.append(
                f"contrefactuel: l'hypothese '{hyp['detail']}' ne reproduit pas "
                "les symptomes — rejetee"
            )

        # ---- 6. Converge ------------------------------------------------
        if confirmed is not None:
            confidence = "elevee" if confirmed.get("aligned") else "moyenne"
            cause = {
                "type": confirmed["type"],
                "object": confirmed["object"],
                "where": confirmed["where"],
                "detail": confirmed["detail"],
            }
        elif hypotheses:
            best = hypotheses[0]
            confidence = "faible"
            cause = {
                "type": best["type"],
                "object": best["object"],
                "where": best["where"],
                "detail": best["detail"] + " (non confirmee par contrefactuel)",
            }
        else:
            confidence = "faible"
            cause = {
                "type": "unknown",
                "object": "unknown",
                "where": "unknown",
                "detail": "aucune cause candidate identifiee",
            }

        yield Conclusion(
            cause=cause,
            confidence=confidence,
            evidence_chain=evidence,
            affected_clients=affected,
            counterfactual=counterfactual,
        )
