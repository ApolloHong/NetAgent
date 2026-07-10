"""EveNgTwin: TwinBackend against a REAL EVE-NG lab + junos-mcp-server.

REAL BACKENDS (API shapes verified against current docs, 2026-07):
  - EVE-NG REST API: POST /api/auth/login {username, password, html5:"-1"},
    GET /api/labs/{lab}/nodes, GET /api/labs/{lab}/topology; every response
    is wrapped in a {code, status, data} envelope.
  - junos-mcp-server (github.com/Juniper/junos-mcp-server, streamable-http,
    default port 30030): tools `get_router_list`, `get_junos_config`,
    `execute_junos_command` (router_name, command), `gather_device_facts`,
    `load_and_commit_config` (router_name, config_text), `junos_config_diff`.

Both endpoints sit behind small transport seams; a FIXTURE transport replays
captured responses from demo/fixtures/eve/ so the adapter runs (and is
tested) fully offline. `--record` captures those fixtures from a live lab.

CRITICAL SEMANTIC CHANGE vs the sim (documented, not hidden):
  - `recompute()` does not exist here: a real device COMPUTES ITS OWN
    forwarding. Reachability and paths are READ from the RIB
    (`show route ... | display json`), never derived by Dijkstra.
  - Link utilisation comes from the TELEMETRY layer (gNMI/NUAR), not from a
    demand matrix: bind a TelemetrySource with `bind_telemetry()`.
  - Per-OD flows are NOT observable on devices. Phase 1 exposes per-prefix
    reachability only and leaves the TRAFFIC-MATRIX gap explicit through the
    `TrafficMatrixProvider` seam below. We do not fake per-OD flows.
  - The temporal-alignment signal for config drift is the REAL Junos commit
    timestamp (`show system commit`), surfaced as `changed_at_commit`;
    `changed_at_tick` is its projection onto this twin's poll timeline.

SAFETY: read-only by default. Phase 2 (fault injection via NETCONF
load/commit, recovery via golden re-commit, i.e. a declarative rollback) is
gated behind `allow_writes=True` (CLI: `--twin eve --allow-writes`) and only
ever targets lab devices listed in the inventory. Never a production box.
"""

from __future__ import annotations

import copy
import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol

from demo.inventory.identity import IdentityMap, link_id
from demo.twin.backend import TwinBackend
from demo.twin.config_store import ConfigStore
from demo.twin.telemetry import TelemetrySource


class EveWriteDisabled(PermissionError):
    """Raised when a write is attempted without --allow-writes."""


# ---------------------------------------------------------------------------
# Transports (the actual HTTP seams; fixtures replay captured payloads)
# ---------------------------------------------------------------------------
class EveTransport(Protocol):
    def get(self, path: str) -> Any:
        """GET /api/{path}; returns the unwrapped `data` payload."""

    def ping(self) -> bool: ...


class McpTransport(Protocol):
    def call(self, tool: str, **arguments: Any) -> Any:
        """Call a junos-mcp-server tool; returns its text/JSON payload."""

    def ping(self) -> bool: ...


class HttpEveTransport:
    """EVE-NG REST over HTTP (lazy httpx; cookie session after login)."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        import httpx  # lazy: optional [eve] extra

        self._client = httpx.Client(base_url=base_url.rstrip("/"), verify=False, timeout=15)
        response = self._client.post(
            "/api/auth/login",
            json={"username": username, "password": password, "html5": "-1"},
        )
        response.raise_for_status()

    def get(self, path: str) -> Any:
        response = self._client.get(f"/api/{path.lstrip('/')}")
        response.raise_for_status()
        envelope = response.json()  # {code, status, data}
        if envelope.get("status") not in (None, "success"):
            raise RuntimeError(f"EVE-NG API error on {path}: {envelope}")
        return envelope.get("data")

    def ping(self) -> bool:
        try:
            self.get("status")
            return True
        except Exception:
            return False


class FixtureEveTransport:
    """Replays captured EVE-NG REST payloads from demo/fixtures/eve/."""

    def __init__(self, fixture_dir: str | Path) -> None:
        self.dir = Path(fixture_dir)

    def get(self, path: str) -> Any:
        name = "nodes" if path.endswith("/nodes") else (
            "topology" if path.endswith("/topology") else path.replace("/", "_")
        )
        envelope = json.loads((self.dir / f"{name}.json").read_text())
        return envelope.get("data")

    def ping(self) -> bool:
        return (self.dir / "nodes.json").exists()


class HttpMcpTransport:
    """junos-mcp-server over MCP streamable-http (JSON-RPC `tools/call`)."""

    def __init__(self, url: str) -> None:
        import httpx  # lazy: optional [eve] extra

        self._client = httpx.Client(timeout=60)
        self._url = url
        self._id = 0

    def call(self, tool: str, **arguments: Any) -> Any:
        self._id += 1
        response = self._client.post(
            self._url,
            json={
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        response.raise_for_status()
        result = response.json()["result"]
        text = result["content"][0]["text"]
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text

    def ping(self) -> bool:
        try:
            self.call("get_router_list")
            return True
        except Exception:
            return False


class FixtureMcpTransport:
    """Replays captured junos-mcp-server responses.

    Layout: demo/fixtures/eve/mcp_responses.json =
      {router: {"get_junos_config": "<display-set text>",
                "show bgp summary | display json": {...},
                "show isis adjacency | display json": {...},
                "show route | display json": {...},
                "show system commit": "<text>"}}
    Phase-2 writes are recorded in-memory so the write code path is testable
    offline (the 'device' remembers the committed text for this process).
    """

    def __init__(self, fixture_path: str | Path) -> None:
        self._responses = json.loads(Path(fixture_path).read_text())
        self._committed: dict[str, list[str]] = {}

    def call(self, tool: str, **arguments: Any) -> Any:
        router = arguments.get("router_name", "")
        if tool == "get_router_list":
            return sorted(self._responses)
        if tool == "get_junos_config":
            base = self._responses[router]["get_junos_config"]
            return "\n".join([base] + self._committed.get(router, []))
        if tool == "load_and_commit_config":
            self._committed.setdefault(router, []).append(arguments["config_text"])
            return "commit complete"
        if tool == "execute_junos_command":
            return self._responses[router][arguments["command"]]
        raise KeyError(f"fixture has no response for tool '{tool}'")

    def ping(self) -> bool:
        return bool(self._responses)


# ---------------------------------------------------------------------------
# Config store over real device configs
# ---------------------------------------------------------------------------
_SET_LINE = re.compile(r"^set\s+(.*)$")


def set_lines_to_paths(config_text: str) -> dict[str, str]:
    """Normalise Junos `display set` lines into the dotted-path space the
    audit and RCA hypothesis mapping already use (sim-compatible).

    'set protocols isis interface ge-0/0/0.0 metric 1000'
        -> {'protocols.isis.interface.ge-0/0/0.metric': '1000'}
    Interface unit suffixes ('.0') are stripped; the last token is the value.
    Later lines override earlier ones (so re-committed snippets win).
    """
    paths: dict[str, str] = {}
    for raw in config_text.splitlines():
        match = _SET_LINE.match(raw.strip())
        if not match:
            continue
        tokens = [re.sub(r"^(\S+?)\.\d+$", r"\1", t) for t in match.group(1).split()]
        if len(tokens) < 2:
            continue
        paths[".".join(tokens[:-1])] = tokens[-1]
    return paths


_COMMIT_LINE = re.compile(
    r"^\s*(\d+)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+\S+\s+by\s+(\S+)"
)


def parse_commit_history(text: str) -> list[dict[str, str]]:
    """Parse `show system commit` text into [{seq, timestamp, user}]."""
    out = []
    for line in text.splitlines():
        match = _COMMIT_LINE.match(line)
        if match:
            out.append(
                {"seq": match.group(1), "timestamp": match.group(2), "user": match.group(3)}
            )
    return out


class EveConfigStore(ConfigStore):
    """ConfigStore over real Junos configs read through junos-mcp-server.

    Golden reference = the generated configs the twin was bootstrapped from
    (committed under demo/fixtures/eve/golden/). The REAL temporal-alignment
    signal is the Junos commit timestamp; `changed_at_tick` is the poll tick
    where the drift was first observed on this twin's timeline.
    """

    def __init__(self, mcp: McpTransport, identity: IdentityMap, golden_dir: str | Path) -> None:
        self._mcp = mcp
        self._identity = identity
        self._golden_dir = Path(golden_dir)
        self._running_text: dict[str, str] = {}
        self._first_seen_tick: dict[tuple[str, str], int] = {}
        self._commit_ts: dict[str, str] = {}

    def refresh(self, tick: int) -> None:
        for device in self._identity.device_names():
            router = self._identity.mcp_for_device(device)
            self._running_text[device] = self._mcp.call("get_junos_config", router_name=router)
            commits = parse_commit_history(
                self._mcp.call("execute_junos_command", router_name=router,
                               command="show system commit")
            )
            if commits:
                self._commit_ts[device] = commits[0]["timestamp"]
            for path in self._diff_paths(device):
                self._first_seen_tick.setdefault((device, path), tick)

    def get_running(self, node: str) -> dict:
        return set_lines_to_paths(self._running_text.get(node, ""))

    def get_golden(self, node: str) -> dict:
        return set_lines_to_paths((self._golden_dir / f"{node}.set").read_text())

    def set(self, node: str, path: str, value: Any, tick: int) -> Any:
        raise EveWriteDisabled(
            "EveConfigStore is read-only; device changes go through "
            "EveNgTwin.commit_config() (Phase 2, --allow-writes)"
        )

    def _diff_paths(self, node: str) -> list[str]:
        golden, running = self.get_golden(node), self.get_running(node)
        return sorted(
            path
            for path in set(golden) | set(running)
            if golden.get(path) != running.get(path)
        )

    def diff(self, node: str) -> list[dict]:
        golden, running = self.get_golden(node), self.get_running(node)
        entries = []
        for path in self._diff_paths(node):
            entries.append(
                {
                    "node": node,
                    "path": path,
                    "golden": golden.get(path),
                    "running": running.get(path),
                    # poll tick where first observed (twin-timeline projection)
                    "changed_at_tick": self._first_seen_tick.get((node, path)),
                    # the REAL alignment signal: Junos commit timestamp
                    "changed_at_commit": self._commit_ts.get(node),
                }
            )
        return entries


# ---------------------------------------------------------------------------
# Traffic-matrix seam (explicitly NOT implemented — no faked flows)
# ---------------------------------------------------------------------------
class TrafficMatrixProvider(ABC):
    """Seam for per-OD demand estimation on a real network.

    Devices expose link counters and RIBs, never per-OD demands. Producing a
    demand matrix (e.g. gravity/least-squares estimation from link loads +
    routing, or NetFlow/IPFIX ingestion) is FUTURE MODELLING WORK; this ABC
    is where it plugs in. Phase 1 refuses to fabricate flows.
    """

    @abstractmethod
    def demands(self) -> list[dict[str, Any]]:
        """[{'src': prefix, 'dst': prefix, 'mbps': float}, ...]"""


class StubTrafficMatrixProvider(TrafficMatrixProvider):
    def demands(self) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "per-OD traffic matrix not available on real devices: estimate it "
            "from telemetry + RIB (future modelling work) and implement "
            "TrafficMatrixProvider"
        )


# ---------------------------------------------------------------------------
# The twin
# ---------------------------------------------------------------------------
class EveNgTwin(TwinBackend):
    def __init__(
        self,
        eve: EveTransport,
        mcp: McpTransport,
        identity: IdentityMap,
        lab: str = "netops/zone1.unl",
        golden_dir: str | Path = "demo/fixtures/eve/golden",
        allow_writes: bool = False,
        traffic_matrix: TrafficMatrixProvider | None = None,
    ) -> None:
        self._eve = eve
        self._mcp = mcp
        self.identity = identity
        self.lab = lab
        self.allow_writes = allow_writes
        self.traffic_matrix = traffic_matrix or StubTrafficMatrixProvider()
        self.config = EveConfigStore(mcp, identity, golden_dir)
        self._telemetry: TelemetrySource | None = None
        self._poll = 0
        self.events: list[dict] = []
        self._link_state: dict[str, bool] = {}
        self._session_state: dict[str, bool] = {}
        self._bgp: list[dict] = []
        self._isis: list[dict] = []
        self._rib: dict[str, list[str]] = {}  # edge device -> reachable ip prefixes
        self._topology_links: list[dict] = []
        self._load_topology()
        self.tick()  # first poll fills all state

    # ---- wiring -------------------------------------------------------
    def bind_telemetry(self, source: TelemetrySource) -> None:
        """Link utilisation comes from the telemetry layer (gNMI/NUAR)."""
        self._telemetry = source

    # ---- topology (EVE-NG REST) ----------------------------------------
    def _load_topology(self) -> None:
        nodes = self._eve.get(f"labs/{self.lab}/nodes")
        self._eve_nodes = {
            str(node_id): payload["name"] for node_id, payload in nodes.items()
        }
        topology = self._eve.get(f"labs/{self.lab}/topology")
        links: list[dict] = []
        for entry in topology:
            # EVE topology entries reference "node{id}" endpoints.
            src = self.identity.device_for_eve_node(entry["source"].removeprefix("node"))
            dst = self.identity.device_for_eve_node(entry["destination"].removeprefix("node"))
            links.append(
                {
                    "id": link_id(src, dst),
                    "a": min(src, dst),
                    "b": max(src, dst),
                    "source_label": entry.get("source_label"),
                    "destination_label": entry.get("destination_label"),
                }
            )
        self._topology_links = sorted(links, key=lambda l: l["id"])

    # ---- polling --------------------------------------------------------
    def tick(self) -> None:
        """One poll cycle: refresh config/control-plane state, derive
        on-change events by diffing against the previous poll."""
        self._poll += 1
        self.config.refresh(self._poll)
        self._refresh_control_plane()

    def _cli(self, device: str, command: str) -> Any:
        return self._mcp.call(
            "execute_junos_command",
            router_name=self.identity.mcp_for_device(device),
            command=command,
        )

    def _refresh_control_plane(self) -> None:
        # IS-IS adjacencies -> link oper state (adjacency Up on both ends).
        isis: list[dict] = []
        adjacency_up: dict[str, bool] = {}
        for device in self.identity.device_names():
            payload = self._cli(device, "show isis adjacency | display json")
            for adj in _junos_list(payload, "isis-adjacency-information", "isis-adjacency"):
                neighbour = _junos_leaf(adj, "system-name")
                state = _junos_leaf(adj, "adjacency-state")
                lid = link_id(device, neighbour)
                up = state == "Up"
                adjacency_up[lid] = adjacency_up.get(lid, True) and up
        for link in self._topology_links:
            lid = link["id"]
            up = adjacency_up.get(lid, True)
            isis.append({"link": lid, "a": link["a"], "b": link["b"],
                         "state": "Up" if up else "Down", "metric": None})
            previous = self._link_state.get(lid)
            if previous is not None and previous != up:
                self._emit("link_up" if up else "link_down", lid)
            self._link_state[lid] = up
        self._isis = isis

        # BGP sessions (edge routers) -> canonical session ids.
        bgp: list[dict] = []
        for prefix in self.identity.prefix_ids():
            edge = self.identity.attach_device_for_prefix(prefix)
            payload = self._cli(edge, "show bgp summary | display json")
            for peer in _junos_list(payload, "bgp-information", "bgp-peer"):
                ce = _junos_leaf(peer, "description")
                state = _junos_leaf(peer, "peer-state")
                sid = f"{edge}~{ce}"
                up = state == "Established"
                bgp.append(
                    {
                        "id": sid,
                        "kind": "ebgp",
                        "local": edge,
                        "peer": ce,
                        "prefix": prefix if ce == (self.identity._px_by_canonical[prefix].ce) else None,
                        "state": state,
                        "transitions_recent": int(_junos_leaf(peer, "flap-count") or 0),
                    }
                )
                previous = self._session_state.get(sid)
                if previous is not None and previous != up:
                    self._emit("session_up" if up else "session_down", sid)
                self._session_state[sid] = up
        # de-duplicate (each edge answered once per attached prefix)
        seen: set[str] = set()
        self._bgp = [s for s in bgp if not (s["id"] in seen or seen.add(s["id"]))]

        # RIB -> per-prefix reachability (READ, never computed).
        rib: dict[str, list[str]] = {}
        edges = sorted({p.attach_device for p in self.identity.prefixes})
        for edge in edges:
            payload = self._cli(edge, "show route | display json")
            destinations = []
            for table in _junos_list(payload, "route-information", "route-table"):
                for route in table.get("rt", []):
                    destinations.append(_junos_leaf(route, "rt-destination"))
            rib[edge] = destinations
        self._rib = rib

    def _emit(self, kind: str, obj: str) -> None:
        self.events.append({"tick": self._poll, "kind": kind, "object": obj, "detail": ""})

    # ---- TwinBackend read API ------------------------------------------
    def get_nodes(self) -> list[str]:
        return self.identity.device_names()

    def get_links(self) -> list[dict[str, Any]]:
        out = []
        for link in self._topology_links:
            lid = link["id"]
            iface = self.identity.interfaces_of_link(lid)
            utilisation = None
            if self._telemetry is not None and iface:
                series = self._telemetry.series(iface[0], "utilisation_pct", 1)
                utilisation = round(series[-1][1], 1) if series else None
            capacity = (
                self.identity.capacity_for_iface(iface[0]) if iface else None
            )
            out.append(
                {
                    "id": lid,
                    "a": link["a"],
                    "b": link["b"],
                    "capacity_mbps": capacity,
                    "offered_mbps": None,  # traffic-matrix gap (see seam above)
                    "utilisation_pct": utilisation,  # from telemetry, or None
                    "saturated": (utilisation or 0) >= 100,
                    "oper_up": self._link_state.get(lid, True),
                    "metric": None,
                }
            )
        return out

    def get_bgp_sessions(self) -> list[dict[str, Any]]:
        return list(self._bgp)

    def get_isis_adjacencies(self) -> list[dict[str, Any]]:
        return list(self._isis)

    def get_flows(self) -> list[dict[str, Any]]:
        """Per-prefix REACHABILITY read from the RIBs. NOT per-OD flows:
        `mbps`/`path` are None by design (traffic-matrix gap, see
        TrafficMatrixProvider). Status is 'ok' when every other edge still
        holds a route for the prefix, 'broken' otherwise."""
        out = []
        for prefix in self.identity.prefix_ids():
            home = self.identity.attach_device_for_prefix(prefix)
            ip = self.identity.ip_for_prefix(prefix)
            others = [e for e in self._rib if e != home]
            reachable_from = [e for e in others if ip in self._rib.get(e, [])]
            ok = len(reachable_from) == len(others)
            out.append(
                {
                    "src": prefix,
                    "dst": None,
                    "mbps": None,  # traffic-matrix gap
                    "path": None,  # paths require RIB next-hop walk per pair
                    "links": [],
                    "status": "ok" if ok else "broken",
                    "reason": "" if ok else "route absente de la RIB d'au moins un PE",
                    "reachable_from": reachable_from,
                }
            )
        return out

    def events_since(self, tick: int) -> list[dict[str, Any]]:
        return [e for e in self.events if e["tick"] >= tick]

    def snapshot(self) -> "EveNgTwin":
        return copy.deepcopy(self)

    # ---- Phase 2: WRITES (twin-only, gated) -----------------------------
    def _require_writes(self) -> None:
        if not self.allow_writes:
            raise EveWriteDisabled(
                "ecritures desactivees: relancer avec --twin eve --allow-writes "
                "(les ecritures ne visent que le laboratoire, jamais la production)"
            )

    def commit_config(self, device: str, config_text: str) -> str:
        """Fault injection: NETCONF load+commit of a `set ...` snippet."""
        self._require_writes()
        result = self._mcp.call(
            "load_and_commit_config",
            router_name=self.identity.mcp_for_device(device),
            config_text=config_text,
        )
        self.tick()  # observe our own change immediately
        return str(result)

    def rollback_to_golden(self, device: str) -> str:
        """Recovery: declaratively re-commit the golden config aspects that
        drifted (equivalent to `rollback`, but idempotent and derived from
        the same golden reference the audit uses)."""
        self._require_writes()
        drift = self.config.diff(device)
        lines = []
        for entry in drift:
            tokens = entry["path"].split(".")
            if entry["golden"] is None:
                lines.append("delete " + " ".join(tokens))
            else:
                lines.append("set " + " ".join(tokens) + f" {entry['golden']}")
        if not lines:
            return "nothing to roll back"
        return self.commit_config(device, "\n".join(lines))

    def validate_via_show(self, device: str, command: str) -> Any:
        """Post-inject / post-recover validation hook (read-only)."""
        return self._cli(device, command)


# ---------------------------------------------------------------------------
# Junos `| display json` helpers (defensive: shapes vary across versions)
# ---------------------------------------------------------------------------
def _junos_list(payload: Any, outer: str, inner: str) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    blocks = payload.get(outer, [])
    if isinstance(blocks, dict):
        blocks = [blocks]
    out: list[dict] = []
    for block in blocks:
        items = block.get(inner, [])
        out.extend(items if isinstance(items, list) else [items])
    return out


def _junos_leaf(item: dict, key: str) -> str | None:
    value = item.get(key)
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0].get("data")
    if isinstance(value, dict):
        return value.get("data")
    return value
