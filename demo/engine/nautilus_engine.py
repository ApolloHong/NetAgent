"""Optional NautilusTrader engine for the telemetry replay/live lane — and
NOTHING else. Install with `pip install .[nautilus]`.

Why it exists: NautilusTrader gives this one lane a production-grade,
event-driven backbone — deterministic ts-ordered replay from a Parquet
catalog, the same Actor code running in backtest and live, timers, and a
message bus. The detection LOGIC is NOT reimplemented here: the
HeartbeatActor calls the exact same pure functions
(demo/heartbeat/checks.py via demo/engine/base.evaluate_tick) and the same
LaneAggregator as the builtin engine, so both engines must emit identical
incidents (enforced by tests/test_engine_parity.py).

LICENSING: nautilus_trader is LGPL-3.0. It is isolated behind this optional
extra so the core project stays permissively licensed; nothing outside this
module imports it, and the import is guarded — without the package the
factory silently falls back to the builtin engine.

API shapes verified against the NautilusTrader docs (master, 2026-07):
  - custom data subclasses `nautilus_trader.core.Data` and implements the
    `ts_event` / `ts_init` properties (UNIX nanoseconds);
  - actors subscribe with `self.subscribe_data(data_type=DataType(Cls))` and
    receive instances in `on_data`;
  - low-level replay: `BacktestEngine()` + `add_actor(...)` +
    `add_data(list, sort=True)` + `run()`; venues/instruments are OPTIONAL
    for custom-data-only runs;
  - persistence: `ParquetDataCatalog(path).write_data([...])`.
Caveat (documented limitation): a full `LiveDataClient` subclass for gNMI
was NOT wired — its factory/config surface was not doc-verified. The live
seam shipped here is `GnmiTelemetryFeed` (pygnmi subscribe -> rate tracking
-> InterfaceTelemetry payloads); adapting it into a LiveDataClient is
deployment wiring on top of an already-verified feed + actor.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Callable, Iterator

from demo.engine.base import (
    LaneAggregator,
    ReplaySource,
    TelemetryReplayEngine,
    evaluate_tick,
)
from demo.heartbeat.checks import Thresholds
from demo.heartbeat.incident import Incident
from demo.inventory.identity import IdentityMap

NAUTILUS_AVAILABLE = importlib.util.find_spec("nautilus_trader") is not None

_NS = 1_000_000_000


class _BufferSource:
    """Tiny TelemetrySource over the actor's received-data buffers, so the
    actor can call the SAME `evaluate_tick` as the builtin engine."""

    def __init__(self) -> None:
        self._buffers: dict[str, dict[str, list[tuple[int, float]]]] = {}

    def append(self, iface: str, tick: int, util: float, errors: float) -> None:
        per_if = self._buffers.setdefault(
            iface, {"utilisation_pct": [], "errors": [], "latency_ms": []}
        )
        per_if["utilisation_pct"].append((tick, util))
        per_if["errors"].append((tick, errors))

    def series(self, interface: str, field: str, window: int) -> list[tuple[int, float]]:
        return self._buffers.get(interface, {}).get(field, [])[-window:]

    def interfaces(self) -> list[str]:
        return sorted(self._buffers)


if NAUTILUS_AVAILABLE:
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.common.actor import Actor
    from nautilus_trader.core import Data
    from nautilus_trader.model.data import DataType

    class InterfaceTelemetry(Data):
        """Custom Nautilus data point: one interface telemetry bucket."""

        def __init__(
            self,
            node: str,
            interface: str,
            link: str,
            util_pct: float,
            errors: float,
            latency_ms: float | None,
            ts_event: int,
            ts_init: int,
        ) -> None:
            self.node = node
            self.interface = interface
            self.link = link
            self.util_pct = util_pct
            self.errors = errors
            self.latency_ms = latency_ms
            self._ts_event = ts_event
            self._ts_init = ts_init

        @property
        def ts_event(self) -> int:
            return self._ts_event

        @property
        def ts_init(self) -> int:
            return self._ts_init

    class HeartbeatActor(Actor):
        """Runs the SHARED detection checks on InterfaceTelemetry buckets.

        Backtest and live are the same code: only the data publisher differs
        (catalog/add_data in replay, GnmiTelemetryFeed live). The periodic
        config-audit patrol of the full heartbeat would use
        `self.clock.set_timer(...)`; this telemetry lane evaluates at each
        completed bucket boundary instead (equivalent cadence in replay).
        """

        def __init__(
            self,
            identity: IdentityMap,
            thresholds: Thresholds,
            tick_of_ns: dict[int, int],
            time_str: Callable[[int], str],
            verbose: bool = False,
        ) -> None:
            super().__init__()
            self._identity = identity
            self._th = thresholds
            self._tick_of_ns = tick_of_ns
            self._buffer = _BufferSource()
            self._aggregator = LaneAggregator(time_str=time_str, verbose=verbose)
            self._time_str = time_str
            self._current_tick: int | None = None
            self.incidents: list[Incident] = []

        def on_start(self) -> None:
            self.subscribe_data(data_type=DataType(InterfaceTelemetry))

        def on_data(self, data) -> None:
            if not isinstance(data, InterfaceTelemetry):
                return
            tick = self._tick_of_ns[data.ts_event]
            if self._current_tick is not None and tick > self._current_tick:
                self._evaluate(self._current_tick)  # bucket complete
            self._current_tick = max(tick, self._current_tick or 0)
            self._buffer.append(data.interface, tick, data.util_pct, data.errors)

        def on_stop(self) -> None:
            if self._current_tick is not None:
                self._evaluate(self._current_tick)
            self.incidents = self._aggregator.incidents

        def _evaluate(self, tick: int) -> None:
            symptoms = evaluate_tick(
                self._buffer, self._identity, tick, self._time_str, self._th
            )
            self._aggregator.observe(tick, symptoms)


class NautilusReplayEngine(TelemetryReplayEngine):
    """Replay a ReplaySource through a Nautilus BacktestEngine.

    Data is auto-sorted by ts_init => ordered deterministic replay; the
    HeartbeatActor then emits the SAME Incident objects as the builtin path.
    """

    name = "nautilus"

    def __init__(
        self,
        source: ReplaySource,
        identity: IdentityMap,
        thresholds: Thresholds | None = None,
        verbose: bool = False,
    ) -> None:
        if not NAUTILUS_AVAILABLE:
            raise ImportError(
                "nautilus_trader is not installed (pip install .[nautilus]); "
                "the factory should have fallen back to the builtin engine"
            )
        self._source = source
        self._identity = identity
        self._th = thresholds or Thresholds()
        self._verbose = verbose

    def _build_data(self) -> tuple[list, dict[int, int], Callable[[int], str]]:
        clock = self._source.make_clock()
        timeline = clock.timestamps
        ns_of_tick = {t + 1: int(ts.timestamp() * _NS) for t, ts in enumerate(timeline)}
        tick_of_ns = {ns: tick for tick, ns in ns_of_tick.items()}
        items = []
        # Read the full recording through the same series API the builtin
        # engine uses, so both engines see identical samples.
        while not clock.exhausted:
            clock.tick()
        for iface in self._source.interfaces():
            device = iface.split(":")[0]
            link = self._identity.link_for_iface(iface)
            errors = dict(self._source.series(iface, "errors", 10**9))
            for tick, util in self._source.series(iface, "utilisation_pct", 10**9):
                ns = ns_of_tick[tick]
                items.append(
                    InterfaceTelemetry(
                        node=device,
                        interface=iface,
                        link=link,
                        util_pct=util,
                        errors=errors.get(tick, 0.0),
                        latency_ms=None,
                        ts_event=ns,
                        ts_init=ns,
                    )
                )
        return items, tick_of_ns, clock.time_str

    def run(self) -> list[Incident]:
        items, tick_of_ns, time_str = self._build_data()
        actor = HeartbeatActor(
            identity=self._identity,
            thresholds=self._th,
            tick_of_ns=tick_of_ns,
            time_str=time_str,
            verbose=self._verbose,
        )
        engine = BacktestEngine()
        engine.add_actor(actor)
        engine.add_data(items, sort=True)  # ts_init order = deterministic replay
        engine.run()
        incidents = actor.incidents
        engine.dispose()
        return incidents

    def write_catalog(self, catalog_path: str) -> int:
        """NUAR history -> ParquetDataCatalog (the documented persistence
        flow: loader -> wrangler -> catalog.write_data; replays then read
        from the catalog instead of rebuilding from the export)."""
        from nautilus_trader.persistence.catalog import ParquetDataCatalog

        items, _, _ = self._build_data()
        ParquetDataCatalog(catalog_path).write_data(items)
        return len(items)


# ---------------------------------------------------------------------------
# Live parity seam: gNMI -> InterfaceTelemetry payloads (pygnmi)
# ---------------------------------------------------------------------------
def parse_gnmi_update(update: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse one pygnmi subscribe2 notification into counter rows (pure,
    unit-testable offline). Expected shape (pygnmi 0.8.x):
      {"update": {"timestamp": <ns>, "prefix": "interfaces/interface[name=X]",
                  "update": [{"path": "state/counters/in-octets", "val": N}]}}
    """
    body = update.get("update") or {}
    prefix = body.get("prefix", "") or ""
    ifname = None
    if "[name=" in prefix:
        ifname = prefix.split("[name=", 1)[1].split("]", 1)[0]
    rows = []
    for entry in body.get("update", []):
        rows.append(
            {
                "ifname": ifname,
                "path": entry.get("path", ""),
                "value": entry.get("val"),
                "timestamp_ns": body.get("timestamp"),
            }
        )
    return rows


class GnmiTelemetryFeed:
    """Live feed: gNMI streaming counters -> InterfaceTelemetry payload dicts.

    REAL BACKEND: pygnmi (verified 0.8.15) —
        with gNMIclient(target=(host, port), username=u, password=p,
                        insecure=True) as gc:
            for update in gc.subscribe2(subscribe={...}): ...
    Counter-to-rate conversion mirrors the NUAR adapter (resets dropped).
    Wrapping this feed as a NautilusTrader LiveDataClient is deployment
    wiring (see module docstring caveat); in that wrapper, each yielded
    payload becomes an InterfaceTelemetry published on the message bus, and
    the identical HeartbeatActor consumes it. Sandbox = real-time replay.
    """

    SUBSCRIPTION = {
        "subscription": [
            {
                "path": "/interfaces/interface/state/counters",
                "mode": "sample",
                "sample_interval": 10 * _NS,  # nanoseconds
            }
        ],
        "mode": "stream",
        "encoding": "json",
    }

    def __init__(self, target: tuple[str, int], username: str, password: str,
                 identity: IdentityMap, device: str) -> None:
        self._target = target
        self._auth = (username, password)
        self._identity = identity
        self._device = device
        self._last: dict[str, tuple[int, int]] = {}  # iface -> (ts_ns, in_octets)

    def stream(self) -> Iterator[dict[str, Any]]:
        from pygnmi.client import gNMIclient  # lazy: [gnmi] extra

        with gNMIclient(
            target=self._target,
            username=self._auth[0],
            password=self._auth[1],
            insecure=True,
        ) as gc:
            for update in gc.subscribe2(subscribe=self.SUBSCRIPTION):
                yield from self._rates(parse_gnmi_update(update))

    def _rates(self, rows: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for row in rows:
            if not row["ifname"] or not row["path"].endswith("in-octets"):
                continue
            canonical = f"{self._device}:{row['ifname']}"
            ts, octets = row["timestamp_ns"], int(row["value"])
            previous = self._last.get(canonical)
            self._last[canonical] = (ts, octets)
            if previous is None:
                continue
            dt = (ts - previous[0]) / _NS
            delta = octets - previous[1]
            if dt <= 0 or delta < 0:  # reset/wrap: drop, like the NUAR adapter
                continue
            capacity = self._identity.capacity_for_iface(canonical) or 0.0
            if capacity <= 0:
                continue
            yield {
                "interface": canonical,
                "link": self._identity.link_for_iface(canonical),
                "util_pct": min(delta * 8 / (dt * capacity * 1e6) * 100.0, 100.0),
                "errors": 0.0,
                "ts_event": ts,
            }
