"""Virtual clock.

All simulation time is driven by integer ticks (1 tick = TICK_SECONDS of
virtual time) so that every run is deterministic and fast. Nothing in the
detection/RCA logic reads the wall clock; wall time is only measured by the
eval harness to report agent latency.

Real backend: in production this is simply real time — gNMI telemetry and
syslog events arrive timestamped, and the heartbeat runs on a scheduler.
"""

from __future__ import annotations

import datetime as _dt

TICK_SECONDS = 5

# Fixed virtual start so printed timestamps are reproducible run-to-run.
_VIRTUAL_START = _dt.datetime(2026, 7, 8, 10, 30, 0)


class VirtualClock:
    """Monotonic tick counter with a human-readable virtual timestamp."""

    def __init__(self) -> None:
        self.tick_no: int = 0

    def tick(self) -> int:
        self.tick_no += 1
        return self.tick_no

    def now(self) -> int:
        return self.tick_no

    def time_str(self, tick: int | None = None) -> str:
        """Format a tick as HH:MM:SS of virtual time (for the French trace)."""
        t = self.tick_no if tick is None else tick
        return (_VIRTUAL_START + _dt.timedelta(seconds=t * TICK_SECONDS)).strftime(
            "%H:%M:%S"
        )


class ReplayClock:
    """Clock that steps through REAL historical timestamps (same seam as
    VirtualClock: tick()/now()/time_str()).

    Used by the telemetry replay lane: `tick()` advances replay time to the
    next recorded timestamp (e.g. NUAR 5-minute buckets), so the heartbeat
    detection checks run over real past traffic. Tick k (1-based) corresponds
    to `timestamps[k-1]`; tick 0 is "before the recording starts".

    REAL BACKEND note: buckets may be irregular (gaps); `seconds_per_tick` is
    therefore the MEDIAN bucket spacing, only used for human-facing latency
    figures, never for detection logic.
    """

    def __init__(self, timestamps: list[_dt.datetime]) -> None:
        if not timestamps:
            raise ValueError("ReplayClock needs at least one timestamp")
        self.timestamps = sorted(set(timestamps))
        self.tick_no: int = 0
        deltas = sorted(
            (b - a).total_seconds()
            for a, b in zip(self.timestamps, self.timestamps[1:])
        )
        self.seconds_per_tick: float = (
            deltas[len(deltas) // 2] if deltas else float(TICK_SECONDS)
        )

    @property
    def total_ticks(self) -> int:
        return len(self.timestamps)

    @property
    def exhausted(self) -> bool:
        return self.tick_no >= len(self.timestamps)

    def tick(self) -> int:
        if self.exhausted:
            raise StopIteration("replay recording exhausted")
        self.tick_no += 1
        return self.tick_no

    def now(self) -> int:
        return self.tick_no

    def timestamp(self, tick: int | None = None) -> _dt.datetime:
        t = self.tick_no if tick is None else tick
        index = min(max(t, 1), len(self.timestamps)) - 1
        return self.timestamps[index]

    def time_str(self, tick: int | None = None) -> str:
        return self.timestamp(tick).strftime("%Y-%m-%d %H:%M:%S")
