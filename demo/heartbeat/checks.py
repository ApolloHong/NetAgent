"""Pure telemetry detection checks, shared by every detection lane.

Consumers (all use EXACTLY these functions — the logic is never duplicated):
  - demo/heartbeat/detector.py       (sim-twin heartbeat patrol)
  - demo/engine/base.py              (builtin telemetry replay engine)
  - demo/engine/nautilus_engine.py   (optional NautilusTrader HeartbeatActor)

Thresholds are a parameter with defaults equal to the historical constants,
so the sim path is byte-identical. On REAL data (NUAR: 5-minute buckets,
daily/weekly seasonality) these values WILL need retuning — override them via
config.yaml `thresholds:` (see demo/config.py); the change-point window in
particular should cover at least a few hours of real buckets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from demo.heartbeat.incident import Symptom
from demo.rca.analysis import change_point
from demo.twin.sim_twin import (
    CONGESTION_PCT,
    ERROR_RATE_THRESHOLD,
    LATENCY_MS_THRESHOLD,
)

Series = list[tuple[int, float]]
TimeStr = Callable[[int], str]


@dataclass(frozen=True)
class Thresholds:
    """Detection tuning knobs (defaults = the sim demo's historical values)."""

    congestion_pct: float = CONGESTION_PCT
    error_rate: float = ERROR_RATE_THRESHOLD
    latency_ms: float = LATENCY_MS_THRESHOLD
    drop_shift_pct: float = 15.0  # utilisation drop flagging traffic loss
    change_point_score: float = 6.0  # min standardized score to trust a shift
    telemetry_window: int = 24  # samples fed to the change-point check
    audit_every: int = 3  # ticks between config audits (detector only)


def utilisation_checks(
    link: str, series: Series, tick: int, time_str: TimeStr, th: Thresholds
) -> list[Symptom]:
    """Static congestion threshold + change-point traffic-drop check."""
    out: list[Symptom] = []
    if not series:
        return out
    last = series[-1][1]
    cp = change_point(series)
    if last >= th.congestion_pct:
        onset = cp["tick"] if cp and cp["shift"] > 0 else tick
        out.append(
            Symptom(
                kind="congestion",
                object=link,
                detail_fr=(
                    f"utilisation lien {link} a {last:.0f}% "
                    f"(seuil {th.congestion_pct:.0f}%), point de rupture a "
                    f"{time_str(onset)}"
                ),
                tick=tick,
                onset_tick=onset,
                value=last,
            )
        )
    if (
        cp
        and cp["score"] >= th.change_point_score
        and cp["shift"] <= -th.drop_shift_pct
    ):
        out.append(
            Symptom(
                kind="traffic_drop",
                object=link,
                detail_fr=(
                    f"chute de trafic sur {link} ({cp['shift']:+.0f} points) "
                    f"a {time_str(cp['tick'])}"
                ),
                tick=tick,
                onset_tick=cp["tick"],
                value=cp["shift"],
            )
        )
    return out


def error_checks(link: str, series: Series, tick: int, th: Thresholds) -> list[Symptom]:
    if not series or series[-1][1] < th.error_rate:
        return []
    cp = change_point(series)
    onset = cp["tick"] if cp and cp["shift"] > 0 else tick
    return [
        Symptom(
            kind="errors",
            object=link,
            detail_fr=(
                f"erreurs en hausse sur {link} "
                f"({series[-1][1]:.0f}/intervalle, seuil {th.error_rate:.0f})"
            ),
            tick=tick,
            onset_tick=onset,
            value=series[-1][1],
        )
    ]


def latency_checks(link: str, series: Series, tick: int, th: Thresholds) -> list[Symptom]:
    if not series or series[-1][1] < th.latency_ms:
        return []
    cp = change_point(series)
    onset = cp["tick"] if cp and cp["shift"] > 0 else tick
    return [
        Symptom(
            kind="latency",
            object=link,
            detail_fr=(
                f"latence anormale sur {link} ({series[-1][1]:.0f} ms, "
                f"seuil {th.latency_ms:.0f} ms)"
            ),
            tick=tick,
            onset_tick=onset,
            value=series[-1][1],
        )
    ]


def telemetry_checks(
    link: str,
    utilisation: Series,
    errors: Series,
    latency: Series,
    tick: int,
    time_str: TimeStr,
    th: Thresholds,
) -> list[Symptom]:
    """All telemetry checks for one interface's series bundle."""
    return (
        utilisation_checks(link, utilisation, tick, time_str, th)
        + error_checks(link, errors, tick, th)
        + latency_checks(link, latency, tick, th)
    )
