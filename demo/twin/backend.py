"""TwinBackend: the seam between this demo and a real digital twin.

REAL BACKEND this interface maps to:
  - Topology / lab lifecycle ......... EVE-NG REST API (vMX VCP+VFP, vQFX
    nodes bootstrapped with a seed config and ZTP over DHCP, where a DHCP
    reservation keyed by MAC maps each node to its generated full config).
  - Config & operational state reads . junos-mcp-server over NETCONF
    (load/commit config, run show/CLI, read BGP state and facts).
  - Streaming telemetry .............. gNMI subscriptions (interface
    counters, utilisation, errors).

The demo implements it with a pure-Python simulation (SimTwin). Swapping in
the real thing means implementing this ABC against those APIs — the
heartbeat, tools and RCA layers only ever talk to this interface.

Safety framing: the read methods are the only thing the heartbeat and RCA
layers use on the LIVE twin. Mutating methods exist for the fault injector
(which drives controlled experiments) and for sandbox copies; nothing here
ever touches a real production network.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TwinBackend(ABC):
    """Abstract view of a network digital twin (one emulated zone)."""

    # ---- lifecycle / time ------------------------------------------------
    @abstractmethod
    def tick(self) -> None:
        """Advance virtual time by one tick (telemetry sampling included)."""

    @abstractmethod
    def snapshot(self) -> "TwinBackend":
        """Deep-copyable snapshot of full state (for sandbox counterfactuals)."""

    # ---- read-only state (used by heartbeat + RCA on the live twin) ------
    @abstractmethod
    def get_nodes(self) -> list[str]: ...

    @abstractmethod
    def get_links(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_bgp_sessions(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_isis_adjacencies(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_flows(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def events_since(self, tick: int) -> list[dict[str, Any]]: ...
