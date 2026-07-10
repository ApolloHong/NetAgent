"""Labelled evaluation dataset: reproducible fault cases with ground truth.

Each case bundles the fault to inject (same structured shape the RCA
hypotheses use), the EXPECTED structured root cause, the EXPECTED affected
customer prefixes (derived from the demand matrix and the topology — see the
load analysis in the README), a human-time baseline in seconds (how long an
on-call engineer typically needs for this diagnosis), and a difficulty tag.

`clean-01` injects nothing: it feeds the detection PRECISION metric (any
incident raised there is a false positive).
"""

from __future__ import annotations

CASES: list[dict] = [
    {
        "case_id": "drift-isis-metric-01",
        "fault_id": "config_drift.isis_metric.core1-core2",
        "fault": {
            "type": "config_drift",
            "object": "isis_metric",
            "where": "core1-core2",
            "params": {"node": "core1", "new_value": 1000},
        },
        "symptom": (
            "Congestion sur le lien core3-core4 et report de trafic, "
            "sans panne materielle"
        ),
        "expected_root_cause": {
            "type": "config_drift",
            "object": "isis_metric",
            "where": "core1-core2",
            "detail": "metrique IS-IS modifiee (10 -> 1000), deroutant le trafic",
        },
        "expected_affected_clients": ["cust-A/24", "cust-C/24", "cust-D/24"],
        "human_baseline_seconds": 900,
        "difficulty": "medium",
    },
    {
        "case_id": "drift-export-policy-02",
        "fault_id": "config_drift.export_policy.edge3",
        "fault": {
            "type": "config_drift",
            "object": "export_policy",
            "where": "edge3",
            "params": {"ce": "ce-C"},
        },
        "symptom": (
            "Chute de trafic et perte de joignabilite du prefixe cust-C/24, "
            "sessions BGP pourtant etablies"
        ),
        "expected_root_cause": {
            "type": "config_drift",
            "object": "export_policy",
            "where": "edge3",
            "detail": "politique d'export BGP supprimee vers ce-C, prefixe retire",
        },
        "expected_affected_clients": ["cust-A/24", "cust-C/24", "cust-D/24"],
        "human_baseline_seconds": 1800,
        "difficulty": "hard",
    },
    {
        "case_id": "linkdown-core2-core4-01",
        "fault_id": "link_down.link.core2-core4",
        "fault": {"type": "link_down", "object": "link", "where": "core2-core4", "params": {}},
        "symptom": (
            "Lien core2-core4 tombe; le trafic se reporte et sature core3-core4"
        ),
        "expected_root_cause": {
            "type": "link_down",
            "object": "link",
            "where": "core2-core4",
            "detail": "panne du lien core2-core4, reroutage et saturation en aval",
        },
        "expected_affected_clients": ["cust-A/24", "cust-B/24", "cust-C/24", "cust-D/24"],
        "human_baseline_seconds": 600,
        "difficulty": "easy",
    },
    {
        "case_id": "linkdown-core1-core3-02",
        "fault_id": "link_down.link.core1-core3",
        "fault": {"type": "link_down", "object": "link", "where": "core1-core3", "params": {}},
        "symptom": "Lien core1-core3 tombe; congestion induite sur core2-core4",
        "expected_root_cause": {
            "type": "link_down",
            "object": "link",
            "where": "core1-core3",
            "detail": "panne du lien core1-core3, reroutage et congestion en aval",
        },
        "expected_affected_clients": ["cust-A/24", "cust-B/24", "cust-C/24", "cust-D/24"],
        "human_baseline_seconds": 600,
        "difficulty": "easy",
    },
    {
        "case_id": "flap-bgp-edge2-ceB-01",
        "fault_id": "session_flap.bgp_session.edge2~ce-B",
        "fault": {
            "type": "session_flap",
            "object": "bgp_session",
            "where": "edge2~ce-B",
            "params": {"halfperiod": 2},
        },
        "symptom": (
            "Session BGP edge2~ce-B instable; retraits de route repetes du "
            "prefixe cust-B/24"
        ),
        "expected_root_cause": {
            "type": "session_flap",
            "object": "bgp_session",
            "where": "edge2~ce-B",
            "detail": "session eBGP instable (flapping), retrait du prefixe cust-B/24",
        },
        "expected_affected_clients": ["cust-B/24", "cust-D/24"],
        "human_baseline_seconds": 700,
        "difficulty": "medium",
    },
    {
        "case_id": "mtu-core1-core3-01",
        "fault_id": "mtu_mismatch.mtu.core1-core3",
        "fault": {
            "type": "mtu_mismatch",
            "object": "mtu",
            "where": "core1-core3",
            "params": {"node": "core1", "new_value": 8000},
        },
        "symptom": "Erreurs en forte hausse sur core1-core3, trames perdues",
        "expected_root_cause": {
            "type": "mtu_mismatch",
            "object": "mtu",
            "where": "core1-core3",
            "detail": "MTU desaccordee sur core1 (9192 -> 8000), grandes trames perdues",
        },
        "expected_affected_clients": ["cust-A/24", "cust-C/24"],
        "human_baseline_seconds": 1200,
        "difficulty": "medium",
    },
    {
        "case_id": "delay-core2-core4-01",
        "fault_id": "delay_loss.link_quality.core2-core4",
        "fault": {
            "type": "delay_loss",
            "object": "link_quality",
            "where": "core2-core4",
            "params": {"delay_ms": 40.0, "loss_pct": 2.0},
        },
        "symptom": (
            "Hausse de latence (+40 ms) et pertes sur core2-core4, sans "
            "evenement ni derive de configuration"
        ),
        "expected_root_cause": {
            "type": "delay_loss",
            "object": "link_quality",
            "where": "core2-core4",
            "detail": "degradation physique du lien core2-core4 (latence/pertes)",
        },
        "expected_affected_clients": ["cust-A/24", "cust-B/24", "cust-D/24"],
        "human_baseline_seconds": 1000,
        "difficulty": "hard",
    },
    {
        "case_id": "clean-01",
        "fault_id": None,
        "fault": None,
        "symptom": "Aucune panne injectee (mesure des faux positifs)",
        "expected_root_cause": None,
        "expected_affected_clients": [],
        "human_baseline_seconds": 0,
        "difficulty": "n/a",
    },
]

# The three shipped end-to-end scenarios map onto labelled cases so the
# narrated demo and the eval share one source of truth.
SCENARIOS: dict[str, str] = {
    "config_drift": "drift-isis-metric-01",
    "link_down_with_congestion": "linkdown-core2-core4-01",
    "bgp_session_flap": "flap-bgp-edge2-ceB-01",
}


def case_by_id(case_id: str) -> dict:
    for case in CASES:
        if case["case_id"] == case_id:
            return case
    raise KeyError(case_id)
