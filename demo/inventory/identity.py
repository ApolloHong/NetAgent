"""Canonical identity/inventory mapping — the glue for every real adapter.

CANONICAL ID SPACE (identical to the sim's conventions, so the sim needs no
translation and existing detection/RCA logic is untouched):

    device     "core1"
    link       "core1-core2"        sorted endpoint names joined by "-"
    interface  "core1:ge-0/0/0"     "<device>:<ifname>"
    prefix     "cust-A/24"

Bijective mappers between that space and the foreign ID spaces:
  - NUAR interface IDs            (historical telemetry warehouse)
  - EVE-NG node ids / node names  (EVE-NG REST API)
  - junos-mcp-server router names (devices.json keys, NETCONF reads)
  - ConfigStore keys              ((device, ifname) pairs)
  - topology graph node/edge ids  (canonical already)

EVERY real adapter must normalise to canonical ids before returning data, so
the twin, telemetry, config store and diagnostic tools all line up.

The mapping is loaded from an inventory file (JSON) which `--record`
produces; `IdentityMap.from_sim_twin()` derives the same mapping from the
simulated zone (used to seed the committed fixture and for sim-side runs).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


class IdentityError(ValueError):
    """Raised on non-bijective (duplicate) or unresolvable identifiers."""


@dataclass(frozen=True)
class DeviceRecord:
    canonical: str  # "core1"
    eve_node_id: str | None = None  # EVE-NG numeric node id, e.g. "1"
    eve_name: str | None = None  # EVE-NG node display name, e.g. "vMX-core1"
    mcp_name: str | None = None  # junos-mcp-server devices.json key
    mgmt_ip: str | None = None


@dataclass(frozen=True)
class InterfaceRecord:
    canonical: str  # "core1:ge-0/0/0"
    device: str  # "core1"
    ifname: str  # "ge-0/0/0"
    link: str  # "core1-core2"
    nuar_id: str | None = None  # NUAR interface identifier
    capacity_mbps: float | None = None


@dataclass(frozen=True)
class PrefixRecord:
    canonical: str  # "cust-A/24"
    attach_device: str  # "edge1"
    ce: str | None = None  # "ce-A"
    ip_prefix: str | None = None  # real routed prefix, e.g. "10.10.1.0/24"


def link_id(a: str, b: str) -> str:
    """Canonical link id from two endpoint device names."""
    return "-".join(sorted((a, b)))


def _unique_index(records: Iterable, key: str, what: str) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for record in records:
        value = getattr(record, key)
        if value is None:
            continue
        if value in index:
            raise IdentityError(f"duplicate {what} '{value}' (mapping must be bijective)")
        index[value] = record
    return index


@dataclass
class IdentityMap:
    devices: list[DeviceRecord] = field(default_factory=list)
    interfaces: list[InterfaceRecord] = field(default_factory=list)
    prefixes: list[PrefixRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._dev_by_canonical = _unique_index(self.devices, "canonical", "device canonical id")
        self._dev_by_eve_id = _unique_index(self.devices, "eve_node_id", "EVE node id")
        self._dev_by_eve_name = _unique_index(self.devices, "eve_name", "EVE node name")
        self._dev_by_mcp = _unique_index(self.devices, "mcp_name", "MCP router name")
        self._if_by_canonical = _unique_index(
            self.interfaces, "canonical", "interface canonical id"
        )
        self._if_by_nuar = _unique_index(self.interfaces, "nuar_id", "NUAR interface id")
        self._px_by_canonical = _unique_index(self.prefixes, "canonical", "prefix")
        for rec in self.interfaces:
            if rec.canonical != f"{rec.device}:{rec.ifname}":
                raise IdentityError(
                    f"interface '{rec.canonical}' does not match device:ifname "
                    f"'{rec.device}:{rec.ifname}'"
                )

    # ---- device lookups ---------------------------------------------
    def device_for_eve_node(self, eve_node_id: str) -> str:
        return self._require(self._dev_by_eve_id, str(eve_node_id), "EVE node id").canonical

    def device_for_eve_name(self, eve_name: str) -> str:
        return self._require(self._dev_by_eve_name, eve_name, "EVE node name").canonical

    def device_for_mcp(self, mcp_name: str) -> str:
        return self._require(self._dev_by_mcp, mcp_name, "MCP router name").canonical

    def mcp_for_device(self, canonical: str) -> str:
        record = self._require(self._dev_by_canonical, canonical, "device")
        if record.mcp_name is None:
            raise IdentityError(f"device '{canonical}' has no MCP router name in the inventory")
        return record.mcp_name

    def device_names(self) -> list[str]:
        return sorted(d.canonical for d in self.devices)

    # ---- interface lookups ------------------------------------------
    def iface_for_nuar(self, nuar_id: str) -> str:
        return self._require(self._if_by_nuar, nuar_id, "NUAR interface id").canonical

    def nuar_for_iface(self, canonical: str) -> str:
        record = self._require(self._if_by_canonical, canonical, "interface")
        if record.nuar_id is None:
            raise IdentityError(f"interface '{canonical}' has no NUAR id in the inventory")
        return record.nuar_id

    def iface_canonical(self, device: str, ifname: str) -> str:
        canonical = f"{device}:{ifname}"
        self._require(self._if_by_canonical, canonical, "interface")
        return canonical

    def link_for_iface(self, canonical: str) -> str:
        return self._require(self._if_by_canonical, canonical, "interface").link

    def capacity_for_iface(self, canonical: str) -> float | None:
        return self._require(self._if_by_canonical, canonical, "interface").capacity_mbps

    def config_key(self, canonical_iface: str) -> tuple[str, str]:
        """ConfigStore addressing: canonical interface -> (device, ifname)."""
        record = self._require(self._if_by_canonical, canonical_iface, "interface")
        return record.device, record.ifname

    def interfaces_of_link(self, link: str) -> list[str]:
        return sorted(r.canonical for r in self.interfaces if r.link == link)

    def link_ids(self) -> list[str]:
        return sorted({r.link for r in self.interfaces})

    # ---- prefix lookups ---------------------------------------------
    def attach_device_for_prefix(self, prefix: str) -> str:
        return self._require(self._px_by_canonical, prefix, "prefix").attach_device

    def prefix_ids(self) -> list[str]:
        return sorted(p.canonical for p in self.prefixes)

    def ip_for_prefix(self, prefix: str) -> str | None:
        return self._require(self._px_by_canonical, prefix, "prefix").ip_prefix

    def prefix_for_ip(self, ip_prefix: str) -> str:
        for record in self.prefixes:
            if record.ip_prefix == ip_prefix:
                return record.canonical
        raise IdentityError(f"unknown IP prefix: '{ip_prefix}' (not in the inventory)")

    # ---- persistence (produced by --record, consumed by adapters) ----
    def to_file(self, path: str | Path) -> None:
        payload = {
            "devices": [asdict(d) for d in self.devices],
            "interfaces": [asdict(i) for i in self.interfaces],
            "prefixes": [asdict(p) for p in self.prefixes],
        }
        Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    @classmethod
    def from_file(cls, path: str | Path) -> "IdentityMap":
        payload = json.loads(Path(path).read_text())
        return cls(
            devices=[DeviceRecord(**d) for d in payload.get("devices", [])],
            interfaces=[InterfaceRecord(**i) for i in payload.get("interfaces", [])],
            prefixes=[PrefixRecord(**p) for p in payload.get("prefixes", [])],
        )

    @classmethod
    def from_sim_twin(cls, twin) -> "IdentityMap":
        """Derive the mapping from the simulated zone (fixture seed and the
        default identity used when no inventory file is given)."""
        from demo.twin.sim_twin import ATTACHMENTS, _CE_OF  # local: avoid cycle

        devices = [
            DeviceRecord(
                canonical=node,
                eve_node_id=str(index + 1),
                eve_name=f"vMX-{node}",
                mcp_name=node,
                mgmt_ip=f"172.20.20.{11 + index}",
            )
            for index, node in enumerate(sorted(twin.nodes))
        ]
        interfaces: list[InterfaceRecord] = []
        counter = 100000
        for lid in sorted(twin.links):
            link = twin.links[lid]
            for node in sorted((link.a, link.b)):
                counter += 1
                interfaces.append(
                    InterfaceRecord(
                        canonical=twin.iface_id(node, lid),
                        device=node,
                        ifname=twin.iface(node, lid),
                        link=lid,
                        nuar_id=f"NUAR:IF:{counter}",
                        capacity_mbps=link.capacity_mbps,
                    )
                )
        prefixes = [
            PrefixRecord(
                canonical=p,
                attach_device=edge,
                ce=_CE_OF[p],
                ip_prefix=f"10.10.{index + 1}.0/24",
            )
            for index, (p, edge) in enumerate(sorted(ATTACHMENTS.items()))
        ]
        return cls(devices=devices, interfaces=interfaces, prefixes=prefixes)

    # ------------------------------------------------------------------
    @staticmethod
    def _require(index: dict, key: str, what: str):
        try:
            return index[key]
        except KeyError:
            raise IdentityError(f"unknown {what}: '{key}' (not in the inventory)") from None
