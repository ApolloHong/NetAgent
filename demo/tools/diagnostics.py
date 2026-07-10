"""Diagnostic tools over the twin (the query-agent capability set + RCA extras).

The first nine tools are read-only views of the LIVE twin — in the real
system they are backed by junos-mcp-server (device reads), the query agent's
path/impact algorithms, and the gNMI collector. The tenth,
`counterfactual_inject`, is the RCA-specific capability: it replays a
hypothesised cause inside a SANDBOX COPY of the healthy baseline and checks
whether the incident's symptom signature reproduces.

SAFETY: none of these tools can mutate the live twin. `counterfactual_inject`
builds its own deep copy per call; `shortest_path(state="baseline")` reads a
private copy of the baseline made at toolset construction.
"""

from __future__ import annotations

from typing import Any

from demo.heartbeat.incident import Incident
from demo.rca.counterfactual import Counterfactual, SimCounterfactual
from demo.tools.registry import ToolRegistry
from demo.twin.sim_twin import ATTACHMENTS, SimTwin

_STATUS_RANK = {"ok": 0, "degraded": 1, "broken": 2}


def _resolve_endpoint(twin: SimTwin, name: str) -> str:
    """Accept a router name or a customer prefix as a path endpoint."""
    if name in ATTACHMENTS:
        return ATTACHMENTS[name]
    if name in twin.nodes:
        return name
    raise ValueError(f"unknown endpoint: {name}")


def _client_impact(twin: SimTwin) -> list[dict[str, Any]]:
    """Per-prefix worst status over the flows it participates in."""
    worst: dict[str, dict[str, Any]] = {
        p: {"prefix": p, "status": "ok", "reason": ""} for p in sorted(ATTACHMENTS)
    }
    for flow in twin.flows:
        for prefix in (flow.src, flow.dst):
            if _STATUS_RANK[flow.status] > _STATUS_RANK[worst[prefix]["status"]]:
                worst[prefix] = {
                    "prefix": prefix,
                    "status": flow.status,
                    "reason": flow.reason,
                }
    return list(worst.values())


def build_toolset(
    twin: SimTwin, incident: Incident, counterfactual: Counterfactual | None = None
) -> ToolRegistry:
    """Bind the ten diagnostic tools to one live twin + one incident.

    `counterfactual` selects the replay oracle behind the
    `counterfactual_inject` tool (see demo/rca/counterfactual.py):
    sim sandbox (default — historical behaviour), Batfish static what-if,
    or EVE lab replay. The tool surface the reasoners see is identical.
    """
    registry = ToolRegistry()
    oracle = counterfactual or SimCounterfactual(twin, incident)
    baseline_view = twin.sandbox()  # private read-only copy of the baseline

    # ------------------------------------------------------------------
    def shortest_path(src: str, dst: str, state: str = "live") -> dict[str, Any]:
        target = baseline_view if state == "baseline" else twin
        s, d = _resolve_endpoint(target, src), _resolve_endpoint(target, dst)
        path, cost = target.spf_path(s, d)
        links = (
            ["-".join(sorted(p)) for p in zip(path, path[1:])] if path else []
        )
        return {"src": src, "dst": dst, "state": state, "path": path, "cost": cost, "links": links}

    registry.register(
        "shortest_path",
        "Compute the IS-IS shortest path between two routers or customer "
        "prefixes, on the live twin or on the pre-incident baseline.",
        {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "router or customer prefix"},
                "dst": {"type": "string", "description": "router or customer prefix"},
                "state": {
                    "type": "string",
                    "enum": ["live", "baseline"],
                    "description": "network state to compute on (default live)",
                },
            },
            "required": ["src", "dst"],
        },
        shortest_path,
    )

    # ------------------------------------------------------------------
    def get_link_traffic(link: str) -> dict[str, Any]:
        l = twin.links[link]
        flows = [
            {"src": f.src, "dst": f.dst, "mbps": f.mbps}
            for f in twin.flows
            if link in f.links
        ]
        return {
            "link": link,
            "oper_up": l.oper_up,
            "capacity_mbps": l.capacity_mbps,
            "offered_mbps": round(l.offered_mbps, 1),
            "utilisation_pct": round(l.utilisation_pct, 1),
            "saturated": l.saturated,
            "flows": sorted(flows, key=lambda f: -f["mbps"]),
        }

    registry.register(
        "get_link_traffic",
        "Current traffic on a link: capacity, offered load, utilisation, "
        "saturation flag and the customer flows crossing it.",
        {
            "type": "object",
            "properties": {"link": {"type": "string", "description": "link id, e.g. core3-core4"}},
            "required": ["link"],
        },
        get_link_traffic,
    )

    # ------------------------------------------------------------------
    def get_affected_clients(scope: str) -> dict[str, Any]:
        impact = _client_impact(twin)
        if scope != "all":
            if scope in twin.links:
                on_scope = {
                    p
                    for f in twin.flows
                    if scope in f.links
                    for p in (f.src, f.dst)
                }
            elif scope in twin.nodes:
                on_scope = {
                    p
                    for f in twin.flows
                    if f.path and scope in f.path
                    for p in (f.src, f.dst)
                }
            else:
                raise ValueError(f"unknown scope: {scope}")
            impact = [c for c in impact if c["prefix"] in on_scope]
        affected = [c for c in impact if c["status"] != "ok"]
        return {"scope": scope, "affected": affected, "all_clients": impact}

    registry.register(
        "get_affected_clients",
        "Return the customer prefixes whose reachability is currently degraded "
        "or broken, scoped to a link, a node, or 'all'.",
        {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "link id, node id, or 'all'"}
            },
            "required": ["scope"],
        },
        get_affected_clients,
    )

    # ------------------------------------------------------------------
    registry.register(
        "get_topology_neighbours",
        "List the topology neighbours of a node (routers and attached CEs), "
        "with the connecting link and its operational state.",
        {
            "type": "object",
            "properties": {"node": {"type": "string", "description": "router name"}},
            "required": ["node"],
        },
        lambda node: {"node": node, "neighbours": twin.neighbours(node)},
    )

    # ------------------------------------------------------------------
    registry.register(
        "get_bgp_state",
        "All BGP sessions (eBGP to CEs and the iBGP mesh) with state, the "
        "prefix each eBGP session advertises, and recent state transitions.",
        {"type": "object", "properties": {}, "required": []},
        lambda: {"sessions": twin.get_bgp_sessions()},
    )

    registry.register(
        "get_isis_adjacencies",
        "All IS-IS adjacencies with state and effective metric per link.",
        {"type": "object", "properties": {}, "required": []},
        lambda: {"adjacencies": twin.get_isis_adjacencies()},
    )

    # ------------------------------------------------------------------
    registry.register(
        "get_device_config",
        "Full running configuration of a device (Junos-shaped structure).",
        {
            "type": "object",
            "properties": {"node": {"type": "string", "description": "router name"}},
            "required": ["node"],
        },
        lambda node: {"node": node, "running": twin.config.get_running(node)},
    )

    registry.register(
        "diff_config",
        "Differences between the running config and the golden reference for "
        "a device, with the commit tick of each change (empty list = no drift).",
        {
            "type": "object",
            "properties": {"node": {"type": "string", "description": "router name"}},
            "required": ["node"],
        },
        lambda node: {"node": node, "diff": twin.config.diff(node)},
    )

    # ------------------------------------------------------------------
    def read_telemetry(interface: str, window: int = 24) -> dict[str, Any]:
        # Accept either an interface id ("core1:ge-0/0/1") or a link id
        # ("core1-core3", resolved to its first endpoint's interface).
        iface = interface
        if ":" not in interface and interface in twin.links:
            link = twin.links[interface]
            iface = twin.iface_id(link.a, interface)
        return {
            "interface": iface,
            "link": twin.link_of_interface(iface),
            "window": window,
            "utilisation_pct": twin.telemetry.series(iface, "utilisation_pct", window),
            "errors": twin.telemetry.series(iface, "errors", window),
            "latency_ms": twin.telemetry.series(iface, "latency_ms", window),
        }

    registry.register(
        "read_telemetry",
        "Time series (tick, value) of utilisation %, error count and latency "
        "ms for an interface (or a link id) over the last `window` ticks.",
        {
            "type": "object",
            "properties": {
                "interface": {
                    "type": "string",
                    "description": "interface id 'node:ifname' or link id",
                },
                "window": {"type": "integer", "description": "samples (default 24)"},
            },
            "required": ["interface"],
        },
        read_telemetry,
    )

    # ------------------------------------------------------------------
    def counterfactual_inject(hypothesis: dict[str, Any]) -> dict[str, Any]:
        # Delegates to the selected oracle (sim sandbox by default; Batfish
        # or EVE lab replay when injected). Same result shape either way.
        return oracle.test(hypothesis)

    registry.register(
        "counterfactual_inject",
        "Replay a hypothesised root cause inside a SANDBOX COPY of the "
        "pre-incident twin and report whether the incident's observed symptom "
        "signature reproduces. The live twin is never touched. The hypothesis "
        "must be a structured cause: {type, object, where, params}.",
        {
            "type": "object",
            "properties": {
                "hypothesis": {
                    "type": "object",
                    "description": (
                        "Structured cause. type: one of link_down, config_drift, "
                        "session_flap, mtu_mismatch, delay_loss; object: e.g. "
                        "isis_metric, export_policy, link, bgp_session, mtu, "
                        "link_quality; where: link/session/node id; params: "
                        "fault-specific (node, new_value, ce, delay_ms, ...)."
                    ),
                }
            },
            "required": ["hypothesis"],
        },
        counterfactual_inject,
    )

    return registry
