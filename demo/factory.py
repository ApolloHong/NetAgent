"""Backend factory: build the selected backends and inject them into the
existing pipeline. The pipeline code (heartbeat, RCA, eval) never knows
which backend it got — it only sees the interfaces.

Fallback policy (hard requirement): every real backend is optional and
gated. If its endpoint/credentials are absent or unreachable, the factory
falls back to the recorded FIXTURES under demo/fixtures/ with an [INFO]
line, so the pipeline still runs and tests still pass offline.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Callable

from demo.clock import TICK_SECONDS, ReplayClock
from demo.config import FIXTURES_DIR, BackendSelection, resolve_secret, thresholds_from
from demo.engine.base import BuiltinReplayEngine, ReplaySource, TelemetryReplayEngine
from demo.heartbeat.checks import Thresholds
from demo.inventory.identity import IdentityMap


def _info(msg: str) -> None:
    print(f"[INFO] {msg}")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def build_identity(selection: BackendSelection) -> IdentityMap:
    path = selection.raw.get("inventory", str(FIXTURES_DIR / "inventory.json"))
    return IdentityMap.from_file(path)


def build_thresholds(selection: BackendSelection) -> Thresholds:
    return thresholds_from(selection.raw)


# ---------------------------------------------------------------------------
# Twin
# ---------------------------------------------------------------------------
def build_eve_twin(selection: BackendSelection, identity: IdentityMap):
    """EveNgTwin with real HTTP transports when configured & reachable,
    otherwise the committed fixtures (offline)."""
    from demo.twin.eve_twin import (
        EveNgTwin,
        FixtureEveTransport,
        FixtureMcpTransport,
        HttpEveTransport,
        HttpMcpTransport,
    )

    eve_cfg = selection.section("eve")
    mcp_cfg = selection.section("mcp")
    eve = mcp = None
    if eve_cfg.get("base_url"):
        try:
            eve = HttpEveTransport(
                base_url=str(eve_cfg["base_url"]),
                username=str(eve_cfg.get("username", "admin")),
                password=resolve_secret(eve_cfg, "password") or "",
            )
            if not eve.ping():
                raise ConnectionError("EVE-NG API unreachable")
        except Exception as exc:
            _info(f"EVE-NG injoignable ({exc}); repli sur les fixtures enregistrees.")
            eve = None
    if mcp_cfg.get("url"):
        try:
            mcp = HttpMcpTransport(url=str(mcp_cfg["url"]))
            if not mcp.ping():
                raise ConnectionError("junos-mcp-server unreachable")
        except Exception as exc:
            _info(f"junos-mcp-server injoignable ({exc}); repli sur les fixtures.")
            mcp = None
    if eve is None:
        eve = FixtureEveTransport(FIXTURES_DIR / "eve")
    if mcp is None:
        mcp = FixtureMcpTransport(FIXTURES_DIR / "eve" / "mcp_responses.json")
    return EveNgTwin(
        eve=eve,
        mcp=mcp,
        identity=identity,
        lab=str(eve_cfg.get("lab", "netops/zone1.unl")),
        golden_dir=selection.raw.get("golden_dir", str(FIXTURES_DIR / "eve" / "golden")),
        allow_writes=selection.allow_writes,
    )


# ---------------------------------------------------------------------------
# Counterfactual oracle
# ---------------------------------------------------------------------------
def build_counterfactual_factory(
    selection: BackendSelection, identity: IdentityMap
) -> Callable | None:
    """Returns a (twin, incident) -> Counterfactual factory, or None for the
    default sim oracle (keeps the historical behaviour byte-identical)."""
    name = selection.counterfactual
    if name == "sim":
        return None

    if name == "batfish":
        from demo.rca.counterfactual import BatfishCounterfactual, FixtureBatfishBackend

        backend = None
        bf_cfg = selection.section("batfish")
        if bf_cfg.get("host"):
            try:
                from demo.rca.counterfactual import PybatfishBackend

                backend = PybatfishBackend(
                    host=str(bf_cfg["host"]),
                    snapshot_dir=str(bf_cfg.get("snapshot_dir", "snapshots/base")),
                    identity=identity,
                )
            except Exception as exc:
                _info(f"Batfish indisponible ({exc}); repli sur les reponses enregistrees.")
        if backend is None:
            backend = FixtureBatfishBackend(FIXTURES_DIR / "batfish" / "answers.json")
        return lambda twin, incident: BatfishCounterfactual(backend, incident)

    if name == "eve":
        from demo.rca.counterfactual import EveCounterfactual

        eve_twin = build_eve_twin(selection, identity)
        return lambda twin, incident: EveCounterfactual(
            eve_twin, allow=selection.allow_writes
        )

    raise ValueError(f"unknown counterfactual oracle: {name}")


# ---------------------------------------------------------------------------
# Telemetry replay lane (source + engine)
# ---------------------------------------------------------------------------
class SimReplaySource:
    """ReplaySource over a recorded SIM run (lets the replay lane and the
    engine parity checks run without any real data). Virtual ticks are
    projected onto synthetic wall-clock timestamps."""

    def __init__(self, twin) -> None:
        start = _dt.datetime(2026, 7, 8, 10, 30, 0)
        self._series: dict[str, dict[str, list[tuple[int, float]]]] = {}
        max_tick = 0
        for iface in twin.telemetry.interfaces():
            bundle = {}
            for field in ("utilisation_pct", "errors", "latency_ms"):
                samples = twin.telemetry.series(iface, field, 10**9)
                bundle[field] = samples
                if samples:
                    max_tick = max(max_tick, samples[-1][0])
            self._series[iface] = bundle
        self.timeline = [
            start + _dt.timedelta(seconds=k * TICK_SECONDS) for k in range(1, max_tick + 1)
        ]
        self._clock: ReplayClock | None = None

    def make_clock(self) -> ReplayClock:
        self._clock = ReplayClock(self.timeline)
        return self._clock

    def series(self, interface: str, field: str, window: int) -> list[tuple[int, float]]:
        samples = self._series.get(interface, {}).get(field, [])
        horizon = self._clock.now() if self._clock is not None else float("inf")
        return [s for s in samples if s[0] <= horizon][-window:]

    def interfaces(self) -> list[str]:
        return sorted(self._series)


def build_replay_source(
    selection: BackendSelection, identity: IdentityMap, export_path: str | None = None
) -> ReplaySource:
    if selection.telemetry == "nuar":
        from demo.twin.nuar_telemetry import NuarTelemetrySource, load_nuar_export

        nuar_cfg = selection.section("nuar")
        path = export_path or nuar_cfg.get(
            "export_path", str(FIXTURES_DIR / "nuar" / "nuar_export.json")
        )
        if not Path(path).exists():
            _info(f"export NUAR introuvable ({path}); repli sur la fixture enregistree.")
            path = str(FIXTURES_DIR / "nuar" / "nuar_export.json")
        return NuarTelemetrySource(load_nuar_export(path), identity)

    # telemetry == "sim": replay the drift scenario's own telemetry
    from demo.faults.catalog import build_fault
    from demo.twin.sim_twin import build_default_twin

    twin = build_default_twin(seed=selection.seed)
    fault = build_fault(
        {
            "type": "config_drift",
            "object": "isis_metric",
            "where": "core1-core2",
            "params": {"node": "core1", "new_value": 1000},
        }
    )
    fault.inject(twin)
    for _ in range(12):
        twin.tick()
    return SimReplaySource(twin)


def build_engine(
    selection: BackendSelection,
    source: ReplaySource,
    identity: IdentityMap,
    thresholds: Thresholds,
    verbose: bool = False,
) -> TelemetryReplayEngine:
    if selection.engine == "nautilus":
        from demo.engine.nautilus_engine import NAUTILUS_AVAILABLE

        if NAUTILUS_AVAILABLE:
            from demo.engine.nautilus_engine import NautilusReplayEngine

            return NautilusReplayEngine(source, identity, thresholds, verbose)
        _info("nautilus_trader absent (pip install .[nautilus]); repli sur le moteur builtin.")
    return BuiltinReplayEngine(source, identity, thresholds, verbose)
