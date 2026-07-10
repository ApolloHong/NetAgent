"""SimTwin: deterministic simulated digital twin of one backbone zone.

REAL BACKEND: an EVE-NG lab of ~8 Juniper vMX (VCP+VFP) and vQFX nodes,
bootstrapped by ZTP over DHCP from production-derived configs, driven over
NETCONF by junos-mcp-server and observed with gNMI streaming telemetry.
Here everything is simulated in-process, behind the same TwinBackend
interface, on a virtual clock with a fixed RNG seed for full determinism.

Topology (8 routers, square core + 4 single-homed edges):

    edge1 -- core1 ---- core2 -- edge2
                |          |
    edge3 -- core3 ---- core4 -- edge4

Customers cust-A/24..cust-D/24 attach at edge1..edge4 via eBGP; prefixes
propagate over an iBGP full mesh between the edges. A static demand matrix
carries traffic over IS-IS shortest paths; link utilisation, flow status and
prefix reachability are recomputed after every state or config mutation.

Modelling simplifications (documented on purpose):
  - IS-IS metrics are per-interface in reality (directional); we use the max
    of both sides as the undirected link weight, so a one-sided metric drift
    reroutes both directions. The story is identical, the code simpler.
  - Equal-cost ties are broken by the lexicographically smallest node path
    (deterministic; a real Junos would ECMP).
  - An MTU mismatch of 9192 vs 8000 keeps the IS-IS adjacency up (hellos are
    padded below 8000) but drops large frames: it shows up as interface
    errors and degraded flows, not as a topology change.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import numpy as np

from demo.clock import VirtualClock
from demo.twin.backend import TwinBackend
from demo.twin.config_store import InMemoryConfigStore
from demo.twin.telemetry import TelemetryStore

# Operational thresholds (shared with the heartbeat so signatures line up).
CONGESTION_PCT = 85.0
ERROR_RATE_THRESHOLD = 50.0
LATENCY_MS_THRESHOLD = 30.0
IMPAIRED_LATENCY_MS = 20.0
IMPAIRED_LOSS_PCT = 1.0
MTU_MISMATCH_ERROR_RATE = 180.0

DEFAULT_MTU = 9192
DEFAULT_METRIC = 10

# Keys of the symptom signature used for counterfactual matching.
SIGNATURE_KEYS = (
    "down_links",
    "congested_links",
    "saturated_links",
    "error_links",
    "impaired_links",
    "down_sessions",
    "unreachable_prefixes",
    "degraded_prefixes",
)


@dataclass
class Link:
    id: str
    a: str
    b: str
    ifname_a: str
    ifname_b: str
    capacity_mbps: float
    base_latency_ms: float
    oper_up: bool = True
    offered_mbps: float = 0.0

    @property
    def utilisation_pct(self) -> float:
        return min(self.offered_mbps / self.capacity_mbps * 100.0, 100.0)

    @property
    def saturated(self) -> bool:
        return self.offered_mbps > self.capacity_mbps


@dataclass
class BgpSession:
    id: str
    kind: str  # "ebgp" | "ibgp"
    local: str
    peer: str
    prefix: str | None  # eBGP: the customer prefix learned on this session
    up: bool = True


@dataclass
class Flow:
    """One customer demand (src prefix <-> dst prefix, aggregated Mbps)."""

    src: str
    dst: str
    mbps: float
    path: list[str] | None = None
    links: list[str] = field(default_factory=list)
    status: str = "ok"  # ok | degraded | broken
    reason: str = ""


# ---------------------------------------------------------------------------
# Topology & demand definition (sized so the shipped scenarios behave: see
# the load analysis in the README).
# ---------------------------------------------------------------------------

_CORE_LINKS = [
    ("core1", "core2", 1000.0, 3.0),
    ("core1", "core3", 1000.0, 3.0),
    ("core2", "core4", 1000.0, 3.0),
    ("core3", "core4", 1000.0, 3.0),
]
_EDGE_LINKS = [
    ("edge1", "core1", 2000.0, 1.0),
    ("edge2", "core2", 2000.0, 1.0),
    ("edge3", "core3", 2000.0, 1.0),
    ("edge4", "core4", 2000.0, 1.0),
]

# Customer prefix -> attachment edge router (via an eBGP session to a CE).
ATTACHMENTS = {
    "cust-A/24": "edge1",
    "cust-B/24": "edge2",
    "cust-C/24": "edge3",
    "cust-D/24": "edge4",
}
_CE_OF = {"cust-A/24": "ce-A", "cust-B/24": "ce-B", "cust-C/24": "ce-C", "cust-D/24": "ce-D"}

# Static demand matrix (Mbps). Baseline core loads: core1-core2 62%,
# core2-core4 72%, core1-core3 20%, core3-core4 30% — all well under 85%.
_DEMANDS = [
    ("cust-A/24", "cust-D/24", 620.0),
    ("cust-C/24", "cust-D/24", 300.0),
    ("cust-A/24", "cust-C/24", 200.0),
    ("cust-B/24", "cust-D/24", 100.0),
]


def _link_id(a: str, b: str) -> str:
    return "-".join(sorted((a, b)))


class SimTwin(TwinBackend):
    """The simulated twin. See module docstring for the model."""

    def __init__(self, seed: int = 2026) -> None:
        self.clock = VirtualClock()
        self.rng = np.random.default_rng(seed)
        self.config = InMemoryConfigStore()
        self.telemetry = TelemetryStore(maxlen=64)
        self.events: list[dict] = []
        # Behavioural (non-config) fault conditions, plain data so snapshots
        # deep-copy them and counterfactual replay reproduces their effects.
        self.conditions: dict[str, dict] = {"session_flap": {}, "link_impair": {}}
        self._baseline: SimTwin | None = None

        self.nodes: list[str] = sorted(
            {a for a, *_ in _CORE_LINKS + _EDGE_LINKS}
            | {b for _, b, *_ in _CORE_LINKS + _EDGE_LINKS}
        )
        self.links: dict[str, Link] = {}
        self._iface_of: dict[tuple[str, str], str] = {}  # (node, link_id) -> ifname
        self._build_links()
        self._build_configs()

        self.sessions: dict[str, BgpSession] = {}
        self._build_sessions()

        self.flows: list[Flow] = [Flow(src, dst, mbps) for src, dst, mbps in _DEMANDS]
        self.recompute()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def _build_links(self) -> None:
        per_node_count: dict[str, int] = {n: 0 for n in self.nodes}
        for a, b, cap, lat in sorted(_CORE_LINKS + _EDGE_LINKS):
            lid = _link_id(a, b)
            ifa = f"ge-0/0/{per_node_count[a]}"
            ifb = f"ge-0/0/{per_node_count[b]}"
            per_node_count[a] += 1
            per_node_count[b] += 1
            self.links[lid] = Link(lid, a, b, ifa, ifb, cap, lat)
            self._iface_of[(a, lid)] = ifa
            self._iface_of[(b, lid)] = ifb

    def _build_configs(self) -> None:
        for node in self.nodes:
            interfaces: dict[str, Any] = {}
            isis_ifs: dict[str, Any] = {}
            for link in self.links.values():
                if node not in (link.a, link.b):
                    continue
                peer = link.b if node == link.a else link.a
                ifname = self._iface_of[(node, link.id)]
                interfaces[ifname] = {
                    "description": f"to {peer}",
                    "mtu": DEFAULT_MTU,
                    "link": link.id,
                }
                isis_ifs[ifname] = {"metric": DEFAULT_METRIC}
            cfg: dict[str, Any] = {
                "system": {"host-name": node},
                "interfaces": interfaces,
                "protocols": {"isis": {"interface": isis_ifs}},
            }
            if node.startswith("edge"):
                prefix = next(p for p, e in ATTACHMENTS.items() if e == node)
                ce = _CE_OF[prefix]
                cfg["protocols"]["bgp"] = {
                    "group": {
                        "CUSTOMERS": {
                            "neighbor": {
                                ce: {"export": "EXPORT-CUST", "prefix": prefix}
                            }
                        }
                    }
                }
                cfg["policy-options"] = {
                    "policy-statement": {"EXPORT-CUST": {"then": "accept"}}
                }
            self.config.load(node, cfg)

    def _build_sessions(self) -> None:
        for prefix, edge in sorted(ATTACHMENTS.items()):
            ce = _CE_OF[prefix]
            sid = f"{edge}~{ce}"
            self.sessions[sid] = BgpSession(sid, "ebgp", edge, ce, prefix)
        edges = sorted(e for e in self.nodes if e.startswith("edge"))
        for i, e1 in enumerate(edges):
            for e2 in edges[i + 1 :]:
                sid = f"ibgp:{e1}~{e2}"
                self.sessions[sid] = BgpSession(sid, "ibgp", e1, e2, None)

    # ------------------------------------------------------------------
    # Config-derived attributes
    # ------------------------------------------------------------------
    def iface(self, node: str, link_id: str) -> str:
        return self._iface_of[(node, link_id)]

    def iface_id(self, node: str, link_id: str) -> str:
        return f"{node}:{self._iface_of[(node, link_id)]}"

    def link_of_interface(self, iface_id: str) -> str | None:
        node, _, ifname = iface_id.partition(":")
        for (n, lid), name in self._iface_of.items():
            if n == node and name == ifname:
                return lid
        return None

    def _side_metric(self, node: str, link: Link) -> int:
        cfg = self.config.get_running(node)
        ifname = self._iface_of[(node, link.id)]
        return int(cfg["protocols"]["isis"]["interface"][ifname]["metric"])

    def effective_metric(self, link: Link) -> int:
        # Simplification: undirected weight = max of both configured sides.
        return max(self._side_metric(link.a, link), self._side_metric(link.b, link))

    def _side_mtu(self, node: str, link: Link) -> int:
        cfg = self.config.get_running(node)
        ifname = self._iface_of[(node, link.id)]
        return int(cfg["interfaces"][ifname]["mtu"])

    def mtu_mismatched(self, link: Link) -> bool:
        return self._side_mtu(link.a, link) != self._side_mtu(link.b, link)

    def prefix_withdrawn(self, prefix: str) -> bool:
        """Withdrawn if its eBGP session is down or its export policy is gone."""
        edge = ATTACHMENTS[prefix]
        ce = _CE_OF[prefix]
        session = self.sessions[f"{edge}~{ce}"]
        if not session.up:
            return True
        cfg = self.config.get_running(edge)
        neighbor = cfg["protocols"]["bgp"]["group"]["CUSTOMERS"]["neighbor"][ce]
        return "export" not in neighbor

    # ------------------------------------------------------------------
    # State mutation entry points (used by the fault injector ONLY; the
    # heartbeat and RCA layers never call these on the live twin)
    # ------------------------------------------------------------------
    def set_link_oper(self, link_id: str, up: bool) -> None:
        link = self.links[link_id]
        if link.oper_up == up:
            return
        link.oper_up = up
        self._emit("link_up" if up else "link_down", link_id)
        self.recompute()

    def set_session_state(self, session_id: str, up: bool) -> None:
        session = self.sessions[session_id]
        if session.up == up:
            return
        session.up = up
        self._emit("session_up" if up else "session_down", session_id)
        self.recompute()

    def apply_config_change(self, node: str, path: str, value: Any) -> Any:
        old = self.config.set(node, path, value, self.clock.now())
        self.recompute()
        return old

    def set_flap_condition(self, session_id: str, halfperiod: int) -> None:
        self.conditions["session_flap"][session_id] = {
            "t0": self.clock.now(),
            "halfperiod": halfperiod,
        }
        self.set_session_state(session_id, up=False)  # phase 0 = down

    def clear_flap_condition(self, session_id: str) -> None:
        self.conditions["session_flap"].pop(session_id, None)
        self.set_session_state(session_id, up=True)

    def set_impair_condition(self, link_id: str, delay_ms: float, loss_pct: float) -> None:
        self.conditions["link_impair"][link_id] = {
            "delay_ms": delay_ms,
            "loss_pct": loss_pct,
        }
        self.recompute()

    def clear_impair_condition(self, link_id: str) -> None:
        self.conditions["link_impair"].pop(link_id, None)
        self.recompute()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def _emit(self, kind: str, obj: str, detail: str = "") -> None:
        self.events.append(
            {"tick": self.clock.now(), "kind": kind, "object": obj, "detail": detail}
        )

    def events_since(self, tick: int) -> list[dict]:
        return [e for e in self.events if e["tick"] >= tick]

    # ------------------------------------------------------------------
    # Recompute: SPF -> flow paths -> link loads -> flow status
    # ------------------------------------------------------------------
    def _routing_graph(self) -> nx.Graph:
        g = nx.Graph()
        g.add_nodes_from(self.nodes)
        for link in self.links.values():
            if link.oper_up:
                g.add_edge(link.a, link.b, weight=self.effective_metric(link), link=link.id)
        return g

    def _spf(self, g: nx.Graph, src: str, dst: str) -> list[str] | None:
        try:
            paths = nx.all_shortest_paths(g, src, dst, weight="weight")
            return min(paths)  # deterministic tie-break (lexicographic)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def spf_path(self, src: str, dst: str) -> tuple[list[str] | None, float | None]:
        g = self._routing_graph()
        path = self._spf(g, src, dst)
        if path is None:
            return None, None
        cost = sum(g[u][v]["weight"] for u, v in zip(path, path[1:]))
        return path, cost

    def recompute(self) -> None:
        g = self._routing_graph()
        for link in self.links.values():
            link.offered_mbps = 0.0

        # 1. Route each demand; withdrawn prefixes carry no traffic.
        for flow in self.flows:
            flow.path, flow.links, flow.status, flow.reason = None, [], "ok", ""
            withdrawn = [p for p in (flow.src, flow.dst) if self.prefix_withdrawn(p)]
            if withdrawn:
                flow.status = "broken"
                flow.reason = f"prefixe retire (BGP): {', '.join(withdrawn)}"
                continue
            src_edge, dst_edge = ATTACHMENTS[flow.src], ATTACHMENTS[flow.dst]
            path = self._spf(g, src_edge, dst_edge)
            if path is None:
                flow.status = "broken"
                flow.reason = "reseau partitionne (aucun chemin IS-IS)"
                continue
            flow.path = path
            flow.links = [_link_id(u, v) for u, v in zip(path, path[1:])]
            for lid in flow.links:
                self.links[lid].offered_mbps += flow.mbps

        # 2. Derived error condition: MTU mismatch drops large frames.
        self.error_links: dict[str, float] = {
            link.id: MTU_MISMATCH_ERROR_RATE
            for link in self.links.values()
            if link.oper_up and self.mtu_mismatched(link)
        }

        # 3. Flow degradation from the links they cross.
        impaired = {
            lid
            for lid, c in self.conditions["link_impair"].items()
            if c["delay_ms"] >= IMPAIRED_LATENCY_MS or c["loss_pct"] >= IMPAIRED_LOSS_PCT
        }
        for flow in self.flows:
            if flow.status != "ok":
                continue
            crossed = [self.links[lid] for lid in flow.links]
            if any(l.saturated for l in crossed):
                flow.status = "broken"
                flow.reason = "lien sature, pertes severes"
            elif any(l.utilisation_pct >= CONGESTION_PCT for l in crossed):
                flow.status = "degraded"
                flow.reason = "traverse un lien congestionne"
            elif any(l.id in self.error_links for l in crossed):
                flow.status = "degraded"
                flow.reason = "erreurs sur le chemin (trames perdues)"
            elif any(l.id in impaired for l in crossed):
                flow.status = "degraded"
                flow.reason = "latence/pertes anormales sur le chemin"

        # 4. iBGP sessions ride the IGP: up iff the edges can still reach
        #    each other. Emit events on derived transitions.
        for session in self.sessions.values():
            if session.kind != "ibgp":
                continue
            up = nx.has_path(g, session.local, session.peer)
            if up != session.up:
                session.up = up
                self._emit("session_up" if up else "session_down", session.id)

    # ------------------------------------------------------------------
    # Tick: flap conditions, telemetry sampling
    # ------------------------------------------------------------------
    def tick(self) -> None:
        self.clock.tick()
        t = self.clock.now()
        for sid, flap in sorted(self.conditions["session_flap"].items()):
            phase_down = ((t - flap["t0"]) // flap["halfperiod"]) % 2 == 0
            if self.sessions[sid].up == phase_down:  # state disagrees with phase
                self.set_session_state(sid, up=not phase_down)
        self._sample_telemetry()

    def _sample_telemetry(self) -> None:
        t = self.clock.now()
        for lid in sorted(self.links):
            link = self.links[lid]
            impair = self.conditions["link_impair"].get(lid, {})
            for node in (link.a, link.b):
                util = 0.0
                if link.oper_up:
                    util = float(
                        np.clip(
                            link.utilisation_pct + self.rng.uniform(-1.5, 1.5), 0, 100
                        )
                    )
                errors = float(self.rng.integers(0, 3))
                if link.oper_up and lid in self.error_links:
                    errors += self.error_links[lid]
                latency = link.base_latency_ms + float(self.rng.uniform(-0.4, 0.4))
                latency += float(impair.get("delay_ms", 0.0))
                self.telemetry.append(
                    self.iface_id(node, lid),
                    t,
                    utilisation_pct=util,
                    errors=errors,
                    latency_ms=latency,
                )

    # ------------------------------------------------------------------
    # Snapshot / baseline (the sandbox seam for counterfactual replay)
    # ------------------------------------------------------------------
    def snapshot(self) -> "SimTwin":
        return copy.deepcopy(self)

    def capture_baseline(self) -> None:
        """Freeze the current (healthy) state as the counterfactual baseline."""
        self._baseline = None  # avoid nesting baselines inside baselines
        self._baseline = copy.deepcopy(self)

    def sandbox(self) -> "SimTwin":
        """Fresh sandbox copy of the healthy baseline (never the live twin)."""
        if self._baseline is None:
            raise RuntimeError("capture_baseline() was never called")
        return copy.deepcopy(self._baseline)

    # ------------------------------------------------------------------
    # Symptom signature (ground truth, noise-free — used for detection
    # cross-checks and counterfactual matching)
    # ------------------------------------------------------------------
    def signature(self) -> dict[str, set]:
        impaired = {
            lid
            for lid, c in self.conditions["link_impair"].items()
            if c["delay_ms"] >= IMPAIRED_LATENCY_MS or c["loss_pct"] >= IMPAIRED_LOSS_PCT
        }
        unreachable = set()
        g = self._routing_graph()
        for prefix, edge in ATTACHMENTS.items():
            others = [e for e in ATTACHMENTS.values() if e != edge]
            if self.prefix_withdrawn(prefix) or not any(
                nx.has_path(g, o, edge) for o in others
            ):
                unreachable.add(prefix)
        return {
            "down_links": {l.id for l in self.links.values() if not l.oper_up},
            "congested_links": {
                l.id
                for l in self.links.values()
                if l.oper_up and l.utilisation_pct >= CONGESTION_PCT
            },
            "saturated_links": {
                l.id for l in self.links.values() if l.oper_up and l.saturated
            },
            "error_links": set(self.error_links),
            "impaired_links": impaired,
            "down_sessions": {s.id for s in self.sessions.values() if not s.up},
            "unreachable_prefixes": unreachable,
            "degraded_prefixes": {
                p for f in self.flows if f.status != "ok" for p in (f.src, f.dst)
            },
        }

    def config_diff_all(self) -> list[dict]:
        out: list[dict] = []
        for node in self.nodes:
            out.extend(self.config.diff(node))
        return out

    def is_clean(self) -> bool:
        """True when no symptom is present and configs match golden."""
        sig = self.signature()
        return not any(sig[k] for k in SIGNATURE_KEYS) and not self.config_diff_all()

    # ------------------------------------------------------------------
    # TwinBackend read API
    # ------------------------------------------------------------------
    def get_nodes(self) -> list[str]:
        return list(self.nodes)

    def get_links(self) -> list[dict[str, Any]]:
        return [
            {
                "id": l.id,
                "a": l.a,
                "b": l.b,
                "capacity_mbps": l.capacity_mbps,
                "offered_mbps": round(l.offered_mbps, 1),
                "utilisation_pct": round(l.utilisation_pct, 1),
                "saturated": l.saturated,
                "oper_up": l.oper_up,
                "metric": self.effective_metric(l),
            }
            for l in (self.links[lid] for lid in sorted(self.links))
        ]

    def get_bgp_sessions(self) -> list[dict[str, Any]]:
        window_start = max(0, self.clock.now() - 12)
        transitions: dict[str, int] = {}
        for e in self.events_since(window_start):
            if e["kind"] in ("session_down", "session_up"):
                transitions[e["object"]] = transitions.get(e["object"], 0) + 1
        return [
            {
                "id": s.id,
                "kind": s.kind,
                "local": s.local,
                "peer": s.peer,
                "prefix": s.prefix,
                "state": "Established" if s.up else "Down",
                "transitions_recent": transitions.get(s.id, 0),
            }
            for s in (self.sessions[sid] for sid in sorted(self.sessions))
        ]

    def get_isis_adjacencies(self) -> list[dict[str, Any]]:
        return [
            {
                "link": l.id,
                "a": l.a,
                "b": l.b,
                "state": "Up" if l.oper_up else "Down",
                "metric": self.effective_metric(l),
            }
            for l in (self.links[lid] for lid in sorted(self.links))
            if l.a.startswith(("core", "edge")) and l.b.startswith(("core", "edge"))
        ]

    def get_flows(self) -> list[dict[str, Any]]:
        return [
            {
                "src": f.src,
                "dst": f.dst,
                "mbps": f.mbps,
                "path": f.path,
                "links": f.links,
                "status": f.status,
                "reason": f.reason,
            }
            for f in self.flows
        ]

    def neighbours(self, node: str) -> list[dict[str, Any]]:
        out = [
            {
                "neighbour": l.b if node == l.a else l.a,
                "link": l.id,
                "oper_up": l.oper_up,
            }
            for l in self.links.values()
            if node in (l.a, l.b)
        ]
        for prefix, edge in ATTACHMENTS.items():
            if edge == node:
                out.append(
                    {"neighbour": _CE_OF[prefix], "link": f"attach:{prefix}", "oper_up": True}
                )
        return sorted(out, key=lambda d: d["neighbour"])


def build_default_twin(seed: int = 2026, warmup_ticks: int = 16) -> SimTwin:
    """Build the zone, warm the telemetry up and freeze the healthy baseline."""
    twin = SimTwin(seed=seed)
    for _ in range(warmup_ticks):
        twin.tick()
    twin.capture_baseline()
    return twin
