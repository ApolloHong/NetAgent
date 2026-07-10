"""Per-device configuration store: golden reference vs running config.

REAL BACKEND this interface maps to:
  - Running config reads/writes ...... junos-mcp-server over NETCONF
    (get-config / load-config / commit on the emulated Juniper nodes).
  - Golden reference & static audit .. the config-generation pipeline that
    produced the twin (production config renders) plus Batfish for static
    analysis / drift detection at scale.

Configs are small Junos-shaped nested dicts. Every mutation is recorded in a
change log with its virtual-time tick — this is the equivalent of Junos
commit history (`show system commit`) and is what lets the RCA layer align
config changes with symptom onset in time.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from typing import Any


class ConfigStore(ABC):
    """Abstract config store (running + golden reference + change history)."""

    @abstractmethod
    def get_running(self, node: str) -> dict: ...

    @abstractmethod
    def get_golden(self, node: str) -> dict: ...

    @abstractmethod
    def set(self, node: str, path: str, value: Any, tick: int) -> Any:
        """Set (or delete when value is None) a dotted path; returns old value."""

    @abstractmethod
    def diff(self, node: str) -> list[dict]:
        """Running-vs-golden differences with the tick each change happened."""


def _walk(cfg: dict, parts: list[str], create: bool = False) -> tuple[dict, str]:
    """Navigate to the parent dict of the last path component."""
    cur = cfg
    for p in parts[:-1]:
        if p not in cur:
            if not create:
                raise KeyError(p)
            cur[p] = {}
        cur = cur[p]
    return cur, parts[-1]


def _flatten(cfg: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested config dict into {dotted.path: leaf_value}."""
    out: dict[str, Any] = {}
    if isinstance(cfg, dict):
        for k in sorted(cfg):
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(cfg[k], key))
    else:
        out[prefix] = cfg
    return out


class InMemoryConfigStore(ConfigStore):
    """Deterministic in-memory implementation (deep-copyable for snapshots)."""

    def __init__(self) -> None:
        self._golden: dict[str, dict] = {}
        self._running: dict[str, dict] = {}
        # Change log entries: {tick, node, path, old, new} — commit history.
        self.change_log: list[dict] = []

    def load(self, node: str, config: dict) -> None:
        """Install a device config; the golden reference is frozen here."""
        self._golden[node] = copy.deepcopy(config)
        self._running[node] = copy.deepcopy(config)

    def get_running(self, node: str) -> dict:
        return self._running[node]

    def get_golden(self, node: str) -> dict:
        return self._golden[node]

    def set(self, node: str, path: str, value: Any, tick: int) -> Any:
        parts = path.split(".")
        parent, leaf = _walk(self._running[node], parts, create=True)
        old = parent.get(leaf)
        if value is None:
            parent.pop(leaf, None)
        else:
            parent[leaf] = value
        self.change_log.append(
            {"tick": tick, "node": node, "path": path, "old": old, "new": value}
        )
        return old

    def changed_at(self, node: str, path: str) -> int | None:
        """Tick of the most recent change to this path (commit timestamp)."""
        for entry in reversed(self.change_log):
            if entry["node"] == node and entry["path"] == path:
                return entry["tick"]
        return None

    def diff(self, node: str) -> list[dict]:
        golden = _flatten(self._golden[node])
        running = _flatten(self._running[node])
        entries: list[dict] = []
        for path in sorted(set(golden) | set(running)):
            g, r = golden.get(path), running.get(path)
            if g != r:
                entries.append(
                    {
                        "node": node,
                        "path": path,
                        "golden": g,
                        "running": r,
                        "changed_at_tick": self.changed_at(node, path),
                    }
                )
        return entries
