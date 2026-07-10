"""Analysis primitives shared by the heartbeat and the RCA reasoners.

- change_point(): a simple, defensible mean-shift detector (max standardized
  split statistic) used to pin the ONSET time of a telemetry deviation. In a
  real system this would run in the telemetry pipeline (or a library like
  ruptures); numpy keeps the demo dependency-free.
- rank_candidates(): turns collected evidence into ranked root-cause
  hypotheses, encoding the cause-vs-effect discipline:
    * temporal alignment — only changes/events at or before symptom onset
      can be causes; anything after is an effect or unrelated;
    * topological/causal typing — only INJECTABLE fault types can be root
      causes; a congested link is not a fault type, hence never a cause,
      always an effect to be explained.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# A change/event may be recorded up to this many ticks before the estimated
# onset and still count as temporally aligned (audit cadence + estimation
# slack). One tick after onset is tolerated: onset estimates are +/-1 tick.
ALIGN_BEFORE_TICKS = 6
ALIGN_AFTER_TICKS = 1


def change_point(
    series: list[tuple[int, float]], min_seg: int = 2
) -> dict[str, Any] | None:
    """Best single mean-shift split of a (tick, value) series.

    Returns {"tick", "score", "shift"} for the split with the maximum
    standardized mean difference, or None if the series is too short.
    The score is |mean_after - mean_before| / (s_before * sqrt(1/n1 + 1/n2)),
    with the pre-change std as the noise scale (floored to avoid blow-ups).
    """
    if len(series) < 2 * min_seg:
        return None
    values = np.array([v for _, v in series], dtype=float)
    n = len(values)
    best: dict[str, Any] | None = None
    for k in range(min_seg, n - min_seg + 1):
        left, right = values[:k], values[k:]
        noise = max(float(np.std(left)), 0.5)
        score = abs(float(right.mean() - left.mean())) / (
            noise * np.sqrt(1.0 / len(left) + 1.0 / len(right))
        )
        if best is None or score > best["score"]:
            best = {
                "tick": series[k][0],
                "score": round(float(score), 1),
                "shift": round(float(right.mean() - left.mean()), 1),
            }
    return best


def temporally_aligned(candidate_tick: int | None, onset_tick: int) -> bool:
    if candidate_tick is None:
        return False
    return (
        onset_tick - ALIGN_BEFORE_TICKS
        <= candidate_tick
        <= onset_tick + ALIGN_AFTER_TICKS
    )


def _hypothesis_from_config_diff(entry: dict[str, Any], link_of_iface) -> dict | None:
    """Map a running-vs-golden diff entry to an injectable hypothesis."""
    path, node = entry["path"], entry["node"]
    if ".metric" in path and "isis" in path:
        ifname = path.split(".")[3]
        return {
            "type": "config_drift",
            "object": "isis_metric",
            "where": link_of_iface(f"{node}:{ifname}"),
            "params": {"node": node, "new_value": entry["running"]},
            "detail": (
                f"metrique IS-IS {entry['golden']} -> {entry['running']} "
                f"sur {node} ({ifname})"
            ),
        }
    if ".mtu" in path:
        ifname = path.split(".")[1]
        return {
            "type": "mtu_mismatch",
            "object": "mtu",
            "where": link_of_iface(f"{node}:{ifname}"),
            "params": {"node": node, "new_value": entry["running"]},
            "detail": f"MTU {entry['golden']} -> {entry['running']} sur {node} ({ifname})",
        }
    if ".export" in path and "bgp" in path:
        # protocols.bgp.group.CUSTOMERS.neighbor.<ce>.export
        ce = path.split(".")[5]
        return {
            "type": "config_drift",
            "object": "export_policy",
            "where": node,
            "params": {"ce": ce},
            "detail": f"politique d'export '{entry['golden']}' supprimee vers {ce} sur {node}",
        }
    return None


def rank_candidates(
    onset_tick: int,
    config_diffs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    bgp_sessions: list[dict[str, Any]],
    impaired_links: list[dict[str, Any]],
    link_of_iface,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rank injectable root-cause hypotheses; also return effect notes.

    Score order encodes prior plausibility: an aligned config change is the
    strongest signal, then hard events (link down), then control-plane
    instability, then localized physical impairment. Misaligned items are
    kept out and reported as notes.
    """
    hypotheses: list[dict[str, Any]] = []
    notes: list[str] = []

    for entry in config_diffs:
        hyp = _hypothesis_from_config_diff(entry, link_of_iface)
        if hyp is None:
            continue
        changed_at = entry.get("changed_at_tick")
        if temporally_aligned(changed_at, onset_tick):
            hyp["score"] = 95.0 - abs(onset_tick - (changed_at or onset_tick))
            hyp["aligned"] = True
            hyp["evidence_tick"] = changed_at
            hypotheses.append(hyp)
        else:
            notes.append(
                f"derive de config sur {entry['node']} ({entry['path']}) "
                f"NON alignee dans le temps (t={changed_at}, debut symptomes t={onset_tick})"
            )

    seen_links: set[str] = set()
    for e in events:
        if e["kind"] == "link_down" and e["object"] not in seen_links:
            seen_links.add(e["object"])
            if temporally_aligned(e["tick"], onset_tick):
                hypotheses.append(
                    {
                        "type": "link_down",
                        "object": "link",
                        "where": e["object"],
                        "params": {},
                        "detail": f"lien {e['object']} tombe (evenement link_down a t={e['tick']})",
                        "score": 90.0,
                        "aligned": True,
                        "evidence_tick": e["tick"],
                    }
                )
            else:
                notes.append(
                    f"evenement link_down {e['object']} hors fenetre temporelle"
                )

    down_events: dict[str, int] = {}
    for e in events:
        if e["kind"] == "session_down":
            down_events[e["object"]] = down_events.get(e["object"], 0) + 1
    for s in bgp_sessions:
        if s["kind"] != "ebgp":
            continue  # iBGP transitions are derived effects of the IGP
        downs = down_events.get(s["id"], 0)
        if downs == 0 and s["state"] != "Down":
            continue
        flapping = downs >= 2 or s["transitions_recent"] >= 3
        hypotheses.append(
            {
                "type": "session_flap",
                "object": "bgp_session",
                "where": s["id"],
                "params": {},
                "detail": (
                    f"session BGP {s['id']} "
                    + ("instable (flapping)" if flapping else "tombee")
                    + f", prefixe {s['prefix']} retire"
                ),
                "score": 85.0,
                "aligned": True,
                "evidence_tick": onset_tick,
            }
        )

    for link in impaired_links:
        hypotheses.append(
            {
                "type": "delay_loss",
                "object": "link_quality",
                "where": link["link"],
                "params": {
                    "delay_ms": link.get("delay_ms", 40.0),
                    "loss_pct": link.get("loss_pct", 2.0),
                },
                "detail": (
                    f"degradation physique du lien {link['link']} "
                    f"(latence +{link.get('delay_ms', 0):.0f} ms)"
                ),
                "score": 60.0,
                "aligned": True,
                "evidence_tick": onset_tick,
            }
        )

    hypotheses.sort(key=lambda h: (-h["score"], h["where"]))
    return hypotheses, notes
