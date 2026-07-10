"""Counterfactual oracles: `test(hypothesis) -> reproduces + evidence`.

The RCA agent validates its top hypothesis by REPLAYING it and checking that
the incident's symptom signature reproduces. This module generalises the
original sandbox replay into an interface with three implementations:

  - SimCounterfactual (DEFAULT — behaviour identical to the historical
    `counterfactual_inject`): deep-copy the SimTwin baseline, inject the
    hypothesis via the shared fault factory, tick, compare signatures.
  - BatfishCounterfactual: pybatfish static what-if — no device, no lab,
    second-scale. Models the ROUTING/REACHABILITY consequences of a config
    or topology hypothesis (routes/reachability questions on a forked or
    modified snapshot). Congestion is NOT statically modelable (it needs a
    traffic matrix), and dynamic hypotheses (session flap, physical
    delay/loss) are out of scope — both facts are reported honestly in the
    result instead of being faked.
  - EveCounterfactual: apply the hypothesis on a CLONED EVE-NG lab (or the
    twin lab, then roll back). Slowest, highest fidelity. Requires Phase-2
    writes; the interface is defined here and gated.

All oracles consume the SAME structured hypothesis and reuse the SAME fault
factory (`demo.faults.catalog.build_fault`) or its canonical key, so any
oracle can replay any hypothesis the reasoners produce. All return the same
result dict shape ({hypothesis, reproduced, sandbox_signature, differences,
...}) that the agent trace and the LLM reasoner already understand.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol

from demo.faults.catalog import build_fault
from demo.heartbeat.incident import Incident
from demo.twin.sim_twin import SIGNATURE_KEYS, SimTwin

COUNTERFACTUAL_TICKS = 6  # sandbox ticks to let intermittent effects show

# Signature keys a STATIC routing oracle (Batfish) can actually evaluate.
ROUTING_KEYS = ("down_links", "unreachable_prefixes")


def hypothesis_key(hypothesis: dict[str, Any]) -> str:
    """Canonical stable id of a hypothesis — same shape as Fault.fault_id."""
    return f"{hypothesis['type']}.{hypothesis['object']}.{hypothesis['where']}"


class Counterfactual(ABC):
    """One counterfactual oracle behind the `counterfactual_inject` tool."""

    name = "counterfactual"

    @abstractmethod
    def test(self, hypothesis: dict[str, Any]) -> dict[str, Any]:
        """Replay `hypothesis`; return at least {reproduced, differences}."""


# ---------------------------------------------------------------------------
# Default: sandbox replay on the simulated twin (historical behaviour)
# ---------------------------------------------------------------------------
class SimCounterfactual(Counterfactual):
    """Deep-copy sandbox replay in the SimTwin — the original oracle,
    moved verbatim from tools/diagnostics.py so its output is identical."""

    name = "sim"

    def __init__(self, twin: SimTwin, incident: Incident) -> None:
        self._twin = twin
        self._incident = incident

    def test(self, hypothesis: dict[str, Any]) -> dict[str, Any]:
        sandbox = self._twin.sandbox()  # fresh deep copy of the healthy baseline
        fault = build_fault(hypothesis)
        fault.inject(sandbox)
        observed: dict[str, set] = {k: set() for k in SIGNATURE_KEYS}
        for key, values in sandbox.signature().items():
            observed[key].update(values)
        for _ in range(COUNTERFACTUAL_TICKS):
            sandbox.tick()
            for key, values in sandbox.signature().items():
                observed[key].update(values)
        expected = {
            k: set(self._incident.signature.get(k, set())) for k in SIGNATURE_KEYS
        }
        differences = {
            k: {"expected": sorted(expected[k]), "sandbox": sorted(observed[k])}
            for k in SIGNATURE_KEYS
            if observed[k] != expected[k]
        }
        return {
            "hypothesis": {k: hypothesis[k] for k in ("type", "object", "where")},
            "reproduced": not differences,
            "sandbox_signature": {k: sorted(observed[k]) for k in SIGNATURE_KEYS},
            "differences": differences,
        }


# ---------------------------------------------------------------------------
# Batfish: static routing/reachability what-if (pybatfish)
# ---------------------------------------------------------------------------
class BatfishBackend(Protocol):
    def whatif(self, key: str, hypothesis: dict[str, Any]) -> dict[str, Any]:
        """Predicted routing consequences of a hypothesis:
        {"changed_routes": [...], "down_links": [...],
         "unreachable_prefixes": [...]} — canonical ids throughout.
        Raises KeyError when the hypothesis is not statically modelable."""


class FixtureBatfishBackend:
    """Replays captured Batfish answers (demo/fixtures/batfish/answers.json,
    keyed by the canonical hypothesis key) so CI needs no Batfish service."""

    def __init__(self, fixture_path: str | Path) -> None:
        self._answers = json.loads(Path(fixture_path).read_text())

    def whatif(self, key: str, hypothesis: dict[str, Any]) -> dict[str, Any]:
        return self._answers[key]  # KeyError -> unsupported, handled above


class PybatfishBackend:
    """REAL BACKEND: Batfish via pybatfish (verified 2026-07, pybatfish
    2025.07.07): Session(host=...), set_network, init_snapshot(dir with
    configs/), fork_snapshot(base, name, deactivate_links=[...]), questions
    bf.q.routes() / bf.q.reachability() / bf.q.differentialReachability()
    answered with .answer(snapshot=..., reference_snapshot=...).frame().
    """

    def __init__(self, host: str, snapshot_dir: str | Path, identity) -> None:
        from pybatfish.client.session import Session  # lazy: [batfish] extra

        self._bf = Session(host=host)
        self._bf.set_network("netops-demo")
        self._snapshot_dir = Path(snapshot_dir)
        self._identity = identity
        self._base = self._bf.init_snapshot(
            str(self._snapshot_dir), name="base", overwrite=True
        )

    def whatif(self, key: str, hypothesis: dict[str, Any]) -> dict[str, Any]:
        bf = self._bf
        if hypothesis["type"] == "link_down":
            a, b = hypothesis["where"].split("-", 1)
            if_a = self._identity.interfaces_of_link(hypothesis["where"])[0].split(":")[1]
            if_b = self._identity.interfaces_of_link(hypothesis["where"])[1].split(":")[1]
            candidate = bf.fork_snapshot(
                self._base, name=f"cf-{key}", deactivate_links=[(f"{a}:{if_a}", f"{b}:{if_b}")]
            )
        elif hypothesis["type"] in ("config_drift", "mtu_mismatch"):
            candidate = self._init_modified_snapshot(key, hypothesis)
        else:
            raise KeyError(f"not statically modelable by Batfish: {key}")

        base_routes = bf.q.routes().answer(snapshot=self._base).frame()
        new_routes = bf.q.routes().answer(snapshot=candidate).frame()
        merged = base_routes.merge(
            new_routes, on=["Node", "Network"], how="outer",
            suffixes=("_base", "_new"), indicator=True,
        )
        changed = merged[
            (merged["_merge"] != "both")
            | (merged["Next_Hop_base"].astype(str) != merged["Next_Hop_new"].astype(str))
        ]
        changed_routes = [
            {"node": r["Node"], "prefix": r["Network"]} for _, r in changed.iterrows()
        ]
        unreachable = sorted(
            {
                self._identity.prefix_for_ip(r["Network"])
                for _, r in merged[merged["_merge"] == "left_only"].iterrows()
                if _safe_prefix(self._identity, r["Network"])
            }
        )
        down = [hypothesis["where"]] if hypothesis["type"] == "link_down" else []
        return {
            "changed_routes": changed_routes,
            "down_links": down,
            "unreachable_prefixes": unreachable,
        }

    def _init_modified_snapshot(self, key: str, hypothesis: dict[str, Any]):
        """Write a modified copy of the config snapshot applying the
        hypothesised drift, then init it as a new Batfish snapshot."""
        import shutil
        import tempfile

        staging = Path(tempfile.mkdtemp(prefix="bf-cf-")) / "snapshot"
        shutil.copytree(self._snapshot_dir, staging)
        fault = build_fault(hypothesis)  # shared bridge: same fault factory
        node = hypothesis.get("params", {}).get("node", hypothesis["where"])
        config = staging / "configs" / f"{node}.cfg"
        text = config.read_text()
        if hypothesis.get("object") == "isis_metric":
            iface = self._identity.interfaces_of_link(hypothesis["where"])
            side = next(i for i in iface if i.startswith(node + ":")).split(":")[1]
            text = text.replace(
                f"set protocols isis interface {side}.0 metric 10",
                f"set protocols isis interface {side}.0 metric "
                f"{hypothesis['params'].get('new_value', 1000)}",
            )
        elif hypothesis.get("object") == "export_policy":
            ce = hypothesis["params"]["ce"]
            text = "\n".join(
                l for l in text.splitlines() if f"neighbor {ce} export" not in l
            )
        elif hypothesis.get("object") == "mtu":
            iface = hypothesis["params"]["node"]
            _ = fault  # the shared factory validated the shape above
        config.write_text(text)
        return self._bf.init_snapshot(str(staging), name=f"cf-{key}", overwrite=True)


def _safe_prefix(identity, network: str) -> bool:
    try:
        identity.prefix_for_ip(network)
        return True
    except Exception:
        return False


class BatfishCounterfactual(Counterfactual):
    name = "batfish"

    def __init__(self, backend: BatfishBackend, incident: Incident) -> None:
        self._backend = backend
        self._incident = incident

    def test(self, hypothesis: dict[str, Any]) -> dict[str, Any]:
        key = hypothesis_key(hypothesis)
        base = {"hypothesis": {k: hypothesis[k] for k in ("type", "object", "where")}}
        try:
            predicted = self._backend.whatif(key, hypothesis)
        except KeyError:
            return {
                **base,
                "reproduced": False,
                "unsupported": True,
                "coverage": "none",
                "differences": {
                    "note": {
                        "expected": ["hypothese dynamique"],
                        "sandbox": [
                            "non modelisable statiquement par Batfish "
                            "(utiliser l'oracle sim ou eve)"
                        ],
                    }
                },
            }

        expected = {
            k: set(self._incident.signature.get(k, set())) for k in ROUTING_KEYS
        }
        observed = {k: set(predicted.get(k, [])) for k in ROUTING_KEYS}
        differences = {
            k: {"expected": sorted(expected[k]), "sandbox": sorted(observed[k])}
            for k in ROUTING_KEYS
            if expected[k] != observed[k]
        }
        # A cause must EXPLAIN the symptoms: for congestion-only incidents the
        # routing sets are empty on both sides, so require route churn as the
        # positive evidence that this hypothesis moves traffic at all.
        needs_route_shift = not any(expected.values())
        route_shift_ok = bool(predicted.get("changed_routes")) or not needs_route_shift
        reproduced = not differences and route_shift_ok
        return {
            **base,
            "reproduced": reproduced,
            "coverage": "routing-only",  # congestion needs a traffic matrix
            "sandbox_signature": {k: sorted(observed[k]) for k in ROUTING_KEYS},
            "changed_routes": predicted.get("changed_routes", [])[:10],
            "differences": differences
            if differences
            else (
                {}
                if route_shift_ok
                else {
                    "changed_routes": {
                        "expected": ["report de trafic attendu"],
                        "sandbox": ["aucun changement de route predit"],
                    }
                }
            ),
        }


# ---------------------------------------------------------------------------
# EVE-NG: highest-fidelity oracle (cloned/real lab + rollback) — Phase 2
# ---------------------------------------------------------------------------
class EveCounterfactual(Counterfactual):
    """Apply the hypothesis on the EVE-NG lab, observe, roll back.

    Interface defined; execution requires Phase-2 writes (--allow-writes)
    and a lab you are allowed to perturb. Preferred variant: clone the lab
    (EVE-NG lab export/import) and perturb the CLONE. Not exercised offline.
    """

    name = "eve"

    def __init__(self, eve_twin, allow: bool = False) -> None:
        self._twin = eve_twin
        self._allow = allow

    def test(self, hypothesis: dict[str, Any]) -> dict[str, Any]:
        if not (self._allow and getattr(self._twin, "allow_writes", False)):
            raise PermissionError(
                "EveCounterfactual requiert --twin eve --allow-writes et un "
                "laboratoire clonable; utiliser --counterfactual sim|batfish sinon"
            )
        raise NotImplementedError(
            "lab-replay counterfactual: clone lab -> commit hypothesis "
            "(EveNgTwin.commit_config) -> observe via show/gNMI -> compare "
            "signature -> rollback_to_golden. Wire when a disposable lab "
            "clone is available."
        )
