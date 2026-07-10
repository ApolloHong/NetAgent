"""Fault catalog: the controlled failures the demo can inject.

Each fault changes twin state/config through the twin's mutation API so the
effects are realistic and coherent (reroutes, congestion, withdrawals,
telemetry deviations) — observable by the heartbeat and diagnosable by RCA.

The catalog doubles as the counterfactual engine: the RCA agent turns a
ranked hypothesis into a Fault via `build_fault()` and re-injects it into a
sandbox copy of the twin to check that the symptoms reproduce.
"""

from __future__ import annotations

from typing import Any

from demo.faults.base import Fault
from demo.twin.sim_twin import DEFAULT_METRIC, DEFAULT_MTU, SimTwin


class LinkDownFault(Fault):
    """Hard failure of a link (fibre cut / interface down)."""

    def __init__(self, link_id: str) -> None:
        self.link_id = link_id
        self.fault_id = f"link_down.link.{link_id}"

    def inject(self, twin: SimTwin) -> None:
        twin.set_link_oper(self.link_id, up=False)

    def recover(self, twin: SimTwin) -> None:
        twin.set_link_oper(self.link_id, up=True)

    def validate(self, twin: SimTwin, expect_active: bool = True) -> bool:
        return twin.links[self.link_id].oper_up != expect_active


class MtuMismatchFault(Fault):
    """One-sided MTU change: audit finding + frame-loss errors on the link."""

    def __init__(self, link_id: str, node: str, new_mtu: int = 8000) -> None:
        self.link_id = link_id
        self.node = node
        self.new_mtu = new_mtu
        self.fault_id = f"mtu_mismatch.mtu.{link_id}"

    def _path(self, twin: SimTwin) -> str:
        return f"interfaces.{twin.iface(self.node, self.link_id)}.mtu"

    def inject(self, twin: SimTwin) -> None:
        twin.apply_config_change(self.node, self._path(twin), self.new_mtu)

    def recover(self, twin: SimTwin) -> None:
        twin.apply_config_change(self.node, self._path(twin), DEFAULT_MTU)

    def validate(self, twin: SimTwin, expect_active: bool = True) -> bool:
        active = self.link_id in twin.error_links and twin.mtu_mismatched(
            twin.links[self.link_id]
        )
        return active == expect_active


class DelayLossFault(Fault):
    """Physical impairment (dirty fibre / faulty optic): latency + loss."""

    def __init__(self, link_id: str, delay_ms: float = 40.0, loss_pct: float = 2.0) -> None:
        self.link_id = link_id
        self.delay_ms = delay_ms
        self.loss_pct = loss_pct
        self.fault_id = f"delay_loss.link_quality.{link_id}"

    def inject(self, twin: SimTwin) -> None:
        twin.set_impair_condition(self.link_id, self.delay_ms, self.loss_pct)

    def recover(self, twin: SimTwin) -> None:
        twin.clear_impair_condition(self.link_id)

    def validate(self, twin: SimTwin, expect_active: bool = True) -> bool:
        active = self.link_id in twin.conditions["link_impair"]
        return active == expect_active


class SessionFlapFault(Fault):
    """BGP session instability: the session cycles down/up every few ticks,
    withdrawing the customer prefix on every down phase."""

    def __init__(self, session_id: str, halfperiod: int = 2) -> None:
        self.session_id = session_id
        self.halfperiod = halfperiod
        self.fault_id = f"session_flap.bgp_session.{session_id}"

    def inject(self, twin: SimTwin) -> None:
        twin.set_flap_condition(self.session_id, self.halfperiod)

    def recover(self, twin: SimTwin) -> None:
        twin.clear_flap_condition(self.session_id)

    def validate(self, twin: SimTwin, expect_active: bool = True) -> bool:
        active = self.session_id in twin.conditions["session_flap"]
        if expect_active:
            # The flap must actually have taken the session down at least once.
            went_down = any(
                e["kind"] == "session_down" and e["object"] == self.session_id
                for e in twin.events
            )
            return active and went_down
        return not active and twin.sessions[self.session_id].up


class IsisMetricDriftFault(Fault):
    """Config drift: an IS-IS metric silently changed on one side of a link.
    Produces both an audit finding AND a behavioural change (traffic reroutes,
    possibly congesting another corridor)."""

    def __init__(self, node: str, link_id: str, new_metric: int = 1000) -> None:
        self.node = node
        self.link_id = link_id
        self.new_metric = new_metric
        self.fault_id = f"config_drift.isis_metric.{link_id}"

    def _path(self, twin: SimTwin) -> str:
        return f"protocols.isis.interface.{twin.iface(self.node, self.link_id)}.metric"

    def inject(self, twin: SimTwin) -> None:
        twin.apply_config_change(self.node, self._path(twin), self.new_metric)

    def recover(self, twin: SimTwin) -> None:
        twin.apply_config_change(self.node, self._path(twin), DEFAULT_METRIC)

    def validate(self, twin: SimTwin, expect_active: bool = True) -> bool:
        metric = twin.effective_metric(twin.links[self.link_id])
        drifted = metric == self.new_metric and bool(twin.config.diff(self.node))
        return drifted == expect_active


class ExportPolicyDriftFault(Fault):
    """Config drift: the BGP export policy towards a CE was removed — the
    customer prefix is silently withdrawn (audit finding + reachability loss)."""

    def __init__(self, node: str, ce: str) -> None:
        self.node = node
        self.ce = ce
        self.fault_id = f"config_drift.export_policy.{node}"
        self._path = f"protocols.bgp.group.CUSTOMERS.neighbor.{ce}.export"

    def inject(self, twin: SimTwin) -> None:
        twin.apply_config_change(self.node, self._path, None)

    def recover(self, twin: SimTwin) -> None:
        twin.apply_config_change(self.node, self._path, "EXPORT-CUST")

    def validate(self, twin: SimTwin, expect_active: bool = True) -> bool:
        neighbor = twin.config.get_running(self.node)["protocols"]["bgp"]["group"][
            "CUSTOMERS"
        ]["neighbor"][self.ce]
        active = "export" not in neighbor
        return active == expect_active


def build_fault(spec: dict[str, Any]) -> Fault:
    """Build a Fault from a structured cause description.

    `spec` is the shared shape used by both the eval dataset (fault to
    inject) and the RCA hypotheses (cause to replay in the sandbox):
      {"type": ..., "object": ..., "where": ..., "params": {...}}
    """
    ftype, where = spec["type"], spec["where"]
    params = spec.get("params", {})
    if ftype == "link_down":
        return LinkDownFault(where)
    if ftype == "delay_loss":
        return DelayLossFault(
            where,
            delay_ms=float(params.get("delay_ms", 40.0)),
            loss_pct=float(params.get("loss_pct", 2.0)),
        )
    if ftype == "session_flap":
        return SessionFlapFault(where, halfperiod=int(params.get("halfperiod", 2)))
    if ftype == "mtu_mismatch":
        return MtuMismatchFault(
            where, node=params["node"], new_mtu=int(params.get("new_value", 8000))
        )
    if ftype == "config_drift":
        if spec.get("object") == "isis_metric":
            return IsisMetricDriftFault(
                node=params["node"],
                link_id=where,
                new_metric=int(params.get("new_value", 1000)),
            )
        if spec.get("object") == "export_policy":
            return ExportPolicyDriftFault(node=where, ce=params["ce"])
    raise ValueError(f"unknown fault spec: {spec}")
