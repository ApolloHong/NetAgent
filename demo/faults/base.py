"""Fault base class: strict INJECT -> RECOVER -> VALIDATE lifecycle.

REAL BACKEND: controlled fault injection on the emulated lab — interface
shutdown / metric commits via junos-mcp-server (NETCONF), impairments via
the EVE-NG link layer (tc netem style). Faults are only ever applied to a
twin (live emulation or a sandbox copy), never to a production network.

Every fault must:
  - carry a stable `fault_id` (used by the eval dataset and the judge),
  - `inject(twin)` and `recover(twin)` idempotently,
  - `validate(twin, expect_active)` confirm the effect is actually present
    after inject (expect_active=True) and actually gone after recover
    (expect_active=False). Never trust an injection blindly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from demo.twin.sim_twin import SimTwin


class Fault(ABC):
    fault_id: str

    @abstractmethod
    def inject(self, twin: SimTwin) -> None: ...

    @abstractmethod
    def recover(self, twin: SimTwin) -> None: ...

    @abstractmethod
    def validate(self, twin: SimTwin, expect_active: bool = True) -> bool: ...

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.fault_id}>"
