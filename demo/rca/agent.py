"""RCA orchestrator: plan -> act -> observe -> refine -> validate -> converge.

REAL BACKEND: this plain Python loop is the demo stand-in for a LangGraph
graph (plan / tool / evaluate / iterate nodes) whose tools are MCP servers.
The loop is reasoner-agnostic: a Reasoner decides the next action (a tool
call, a narration note, or a conclusion); the agent executes tools against
the registry, records observations, and narrates the trace in French.

SAFETY: the agent holds only the diagnostic tool registry — every tool is
read-only on the live twin, and counterfactual experiments run in sandbox
copies. The RCA layer never changes the live twin's configuration.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from demo.heartbeat.incident import Incident
from demo.tools.registry import ToolError, ToolRegistry
from demo.twin.sim_twin import SimTwin

MAX_STEPS = 40


# ---------------------------------------------------------------------------
# Actions a reasoner can emit
# ---------------------------------------------------------------------------
@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    note_fr: str | None = None  # optional plan/hypothesis narration


@dataclass
class Note:
    text_fr: str


@dataclass
class Conclusion:
    cause: dict[str, Any]  # {type, object, where, detail}
    confidence: str  # "elevee" | "moyenne" | "faible"
    evidence_chain: list[str]
    affected_clients: list[dict[str, Any]]
    counterfactual: dict[str, Any] | None


@dataclass
class AgentState:
    """What the reasoner can see: the incident plus its own observations."""

    incident: Incident
    nodes: list[str]
    link_of_iface: Callable[[str], str | None]
    observations: list[tuple[str, dict, Any]] = field(default_factory=list)
    last_result: Any = None


class Reasoner(ABC):
    """One reasoning engine behind the orchestrator (rules or LLM)."""

    name = "reasoner"

    @abstractmethod
    def next_action(self, state: AgentState) -> ToolCall | Note | Conclusion: ...

    def plan_fr(self, incident: Incident) -> str:
        return (
            "plan: correler avec les changements de config recents, localiser "
            "sur la topologie, verifier le plan de controle, puis valider la "
            "cause par contrefactuel dans le bac a sable."
        )


@dataclass
class RootCauseReport:
    incident_id: str
    cause: dict[str, Any]
    confidence: str
    evidence_chain: list[str]
    affected_clients: list[dict[str, Any]]
    counterfactual: dict[str, Any] | None
    steps: int
    tool_calls: int
    wall_seconds: float
    reasoner: str

    def print_fr(self) -> None:
        clients = ", ".join(c["prefix"] for c in self.affected_clients) or "aucun"
        print(
            f"[RESULTAT] Cause racine: {self.cause.get('detail', self.cause)}. "
            f"Clients impactes: {clients}. Confiance: {self.confidence}."
        )
        print("[RESULTAT] Chaine de preuves:")
        for i, item in enumerate(self.evidence_chain, 1):
            print(f"  {i}. {item}")


class RcaAgent:
    def __init__(
        self,
        twin: SimTwin,
        incident: Incident,
        reasoner: Reasoner,
        registry: ToolRegistry,
        verbose: bool = True,
    ) -> None:
        self.twin = twin
        self.incident = incident
        self.reasoner = reasoner
        self.registry = registry
        self.verbose = verbose

    def run(self) -> RootCauseReport:
        t0 = time.perf_counter()
        state = AgentState(
            incident=self.incident,
            nodes=self.twin.get_nodes(),
            link_of_iface=self.twin.link_of_interface,
        )
        self._say(self.reasoner.plan_fr(self.incident))

        steps = tool_calls = 0
        conclusion: Conclusion | None = None
        while steps < MAX_STEPS:
            steps += 1
            action = self.reasoner.next_action(state)
            if isinstance(action, Conclusion):
                conclusion = action
                break
            if isinstance(action, Note):
                self._say(action.text_fr)
                state.last_result = None
                continue
            if action.note_fr:
                self._say(action.note_fr)
            try:
                result = self.registry.call(action.tool, action.args)
            except (ToolError, ValueError, KeyError) as exc:
                result = {"error": str(exc)}
            tool_calls += 1
            state.observations.append((action.tool, action.args, result))
            state.last_result = result
            summary = summarize_result_fr(action.tool, action.args, result, self.twin)
            if summary:
                self._say(summary)

        if conclusion is None:  # reasoner never converged: degrade gracefully
            conclusion = Conclusion(
                cause={"type": "unknown", "object": "unknown", "where": "unknown",
                       "detail": "cause non determinee (budget d'etapes epuise)"},
                confidence="faible",
                evidence_chain=["le raisonneur n'a pas converge dans le budget d'etapes"],
                affected_clients=[],
                counterfactual=None,
            )

        return RootCauseReport(
            incident_id=self.incident.id,
            cause=conclusion.cause,
            confidence=conclusion.confidence,
            evidence_chain=conclusion.evidence_chain,
            affected_clients=conclusion.affected_clients,
            counterfactual=conclusion.counterfactual,
            steps=steps,
            tool_calls=tool_calls,
            wall_seconds=round(time.perf_counter() - t0, 3),
            reasoner=self.reasoner.name,
        )

    def _say(self, msg: str) -> None:
        if self.verbose:
            print(f"[RCA] {msg}")


# ---------------------------------------------------------------------------
# Compact French one-liners for the narrated trace
# ---------------------------------------------------------------------------
def _args_short(args: dict[str, Any]) -> str:
    parts = []
    for v in args.values():
        if isinstance(v, dict):
            parts.append(v.get("where", v.get("type", "...")))
        else:
            parts.append(str(v))
    return ", ".join(parts)


def summarize_result_fr(
    tool: str, args: dict[str, Any], result: Any, twin: SimTwin
) -> str | None:
    head = f"{tool}({_args_short(args)}) -> "
    if isinstance(result, dict) and "error" in result:
        return head + f"erreur: {result['error']}"

    if tool == "diff_config":
        diff = result["diff"]
        if not diff:
            return None  # keep the trace legible: no drift, no line
        items = []
        for d in diff:
            when = (
                twin.clock.time_str(d["changed_at_tick"])
                if d.get("changed_at_tick") is not None
                else "?"
            )
            items.append(
                f"{d['path']}: {d['golden']} -> {d['running']} (commit a {when})"
            )
        return head + "; ".join(items)

    if tool == "read_telemetry":
        util = result["utilisation_pct"]
        lat = result["latency_ms"]
        err = result["errors"]
        last = (
            f"util {util[-1][1]:.0f}%" if util else "pas d'echantillon"
        )
        if lat:
            last += f", latence {lat[-1][1]:.0f} ms"
        if err:
            last += f", erreurs {err[-1][1]:.0f}/intervalle"
        return head + f"{len(util)} echantillons sur {result['interface']}; dernier: {last}"

    if tool == "get_bgp_state":
        bad = [
            s
            for s in result["sessions"]
            if s["state"] != "Established" or s["transitions_recent"] >= 2
        ]
        if not bad:
            return head + "toutes les sessions BGP sont etablies et stables"
        return head + "; ".join(
            f"{s['id']} {s['state']} ({s['transitions_recent']} transitions recentes"
            + (f", prefixe {s['prefix']})" if s["prefix"] else ")")
            for s in bad
        )

    if tool == "get_isis_adjacencies":
        down = [a for a in result["adjacencies"] if a["state"] != "Up"]
        odd = [a for a in result["adjacencies"] if a["metric"] != 10]
        msg = "toutes les adjacences IS-IS sont Up" if not down else "adjacences Down: " + ", ".join(a["link"] for a in down)
        if odd:
            msg += "; metriques inhabituelles: " + ", ".join(
                f"{a['link']}={a['metric']}" for a in odd
            )
        return head + msg

    if tool == "get_link_traffic":
        flows = ", ".join(f"{f['src']}<->{f['dst']} {f['mbps']:.0f}M" for f in result["flows"])
        return head + (
            f"{result['utilisation_pct']:.0f}% "
            f"({result['offered_mbps']:.0f}/{result['capacity_mbps']:.0f} Mbps)"
            + (", SATURE" if result["saturated"] else "")
            + (f"; flux: {flows}" if flows else "; aucun flux")
        )

    if tool == "get_affected_clients":
        affected = result["affected"]
        if not affected:
            return head + "aucun client impacte"
        return head + "clients impactes: " + ", ".join(
            f"{c['prefix']} ({c['status']}: {c['reason']})" for c in affected
        )

    if tool == "get_topology_neighbours":
        return head + "voisins: " + ", ".join(
            n["neighbour"] + ("" if n["oper_up"] else " (lien DOWN)")
            for n in result["neighbours"]
        )

    if tool == "shortest_path":
        if not result["path"]:
            return head + "AUCUN chemin (partition ou prefixe retire)"
        return head + (
            f"chemin ({result['state']}): "
            + " > ".join(result["path"])
            + f" (cout {result['cost']:.0f})"
        )

    if tool == "get_device_config":
        return head + f"configuration de {result['node']} recuperee"

    if tool == "counterfactual_inject":
        if result["reproduced"]:
            return head + (
                "en rejouant cette cause dans le bac a sable, les symptomes "
                "observes se REPRODUISENT. Cause racine confirmee."
            )
        diffs = "; ".join(
            f"{k}: attendu {v['expected']}, obtenu {v['sandbox']}"
            for k, v in result["differences"].items()
        )
        return head + f"symptomes NON reproduits ({diffs}). Hypothese rejetee."

    return head + "ok"
