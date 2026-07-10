"""CLI entrypoint.

Offline defaults (unchanged, zero env vars, zero network):
    python -m demo run --scenario config_drift
    python -m demo run --scenario link_down_with_congestion
    python -m demo run --scenario bgp_session_flap
    python -m demo eval

Backend selection (per layer, defaults = sim; also settable in config.yaml):
    --twin {sim,eve}            --telemetry {sim,nuar}
    --counterfactual {sim,batfish,eve}
    --engine {builtin,nautilus}   (telemetry replay/live lane only)
    --reasoner {rules,llm}        (unchanged)
    --config PATH  --allow-writes (Phase-2 EVE writes; lab only)

Extra lanes:
    python -m demo replay [--telemetry nuar] [--engine builtin|nautilus]
        detection over REAL historical telemetry (NUAR buckets)
    python -m demo status [--twin sim|eve]
        read-only state summary (EVE runs offline from recorded fixtures)
    python -m demo record --target {eve,nuar} [--out DIR]
        capture fixtures from real endpoints for later offline replay
"""

from __future__ import annotations

import argparse
import sys

from demo.config import load_config, make_selection
from demo.eval.dataset import SCENARIOS, case_by_id
from demo.eval.runner import run_case, run_eval


def _add_backend_flags(parser: argparse.ArgumentParser, engine: bool = False) -> None:
    parser.add_argument("--config", default=None, metavar="PATH",
                        help="config.yaml/json (default: ./config.yaml if present)")
    parser.add_argument("--twin", choices=["sim", "eve"], default=None)
    parser.add_argument("--telemetry", choices=["sim", "nuar"], default=None)
    parser.add_argument("--counterfactual", choices=["sim", "batfish", "eve"], default=None)
    parser.add_argument("--reasoner", choices=["rules", "llm"], default=None)
    parser.add_argument("--allow-writes", dest="allow_writes", action="store_true",
                        default=None, help="enable Phase-2 EVE lab writes (never prod)")
    parser.add_argument("--seed", type=int, default=None)
    if engine:
        parser.add_argument("--engine", choices=["builtin", "nautilus"], default=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m demo",
        description="Digital-twin incident detection + root-cause analysis demo",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run one narrated end-to-end scenario")
    p_run.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    p_run.add_argument("--png", nargs="?", const="topology.png", default=None,
                       metavar="PATH")
    _add_backend_flags(p_run)

    p_eval = sub.add_parser("eval", help="run the labelled evaluation suite")
    p_eval.add_argument("--llm-judge", action="store_true")
    _add_backend_flags(p_eval)

    p_replay = sub.add_parser(
        "replay", help="telemetry replay lane: detection over recorded history"
    )
    p_replay.add_argument("--export", default=None, metavar="PATH",
                          help="NUAR export file (default: committed fixture)")
    p_replay.add_argument("--quiet", action="store_true")
    _add_backend_flags(p_replay, engine=True)

    p_status = sub.add_parser("status", help="read-only twin state summary")
    _add_backend_flags(p_status)

    p_record = sub.add_parser("record", help="capture fixtures from real endpoints")
    p_record.add_argument("--target", choices=["eve", "nuar"], required=True)
    p_record.add_argument("--out", default="demo/fixtures", metavar="DIR")
    _add_backend_flags(p_record)

    args = parser.parse_args(argv)
    selection = make_selection(args, load_config(getattr(args, "config", None)))

    from demo import factory

    identity = factory.build_identity(selection)

    if args.command in ("run", "eval"):
        if selection.twin == "eve":
            print(
                "[ERREUR] le pipeline scenario/eval sur le jumeau EVE reel exige un "
                "laboratoire accessible en ecriture (Phase 2: --allow-writes + "
                "eve.base_url/mcp.url joignables) — l'injection de pannes ne se "
                "simule pas sur des fixtures statiques. Voir `python -m demo "
                "status --twin eve` pour la lecture seule, et le README pour la "
                "mise en service Phase 2."
            )
            return 2
        if selection.telemetry != "sim":
            print(
                "[ERREUR] --telemetry nuar ne s'applique qu'a la voie replay "
                "(`python -m demo replay`): le pipeline scenario/eval utilise la "
                "telemetrie du jumeau simule."
            )
            return 2
        counterfactual_factory = factory.build_counterfactual_factory(selection, identity)

    if args.command == "run":
        case = case_by_id(SCENARIOS[args.scenario])
        result = run_case(
            case,
            seed=selection.seed,
            reasoner=selection.reasoner,
            verbose=True,
            png_path=args.png,
            counterfactual_factory=counterfactual_factory,
        )
        ok = (
            result.detected
            and result.diagnosis_pass
            and result.clients_pass
            and result.recover_ok
            and result.clean_ok
            and not result.error
        )
        return 0 if ok else 1

    if args.command == "eval":
        results = run_eval(
            reasoner=selection.reasoner,
            seed=selection.seed,
            use_llm_judge=args.llm_judge,
            counterfactual_factory=counterfactual_factory,
        )
        ok = all(
            (not r.expected_detection or (r.detected and r.diagnosis_pass))
            and not r.error
            for r in results
        )
        return 0 if ok else 1

    if args.command == "replay":
        if getattr(args, "telemetry", None) is None and "telemetry" not in selection.raw:
            selection.telemetry = "nuar"  # the natural default for this lane
        thresholds = factory.build_thresholds(selection)
        source = factory.build_replay_source(selection, identity, export_path=args.export)
        engine = factory.build_engine(
            selection, source, identity, thresholds, verbose=not args.quiet
        )
        print(
            f"[REPLAY] moteur={engine.name} telemetrie={selection.telemetry} "
            f"({len(source.interfaces())} interfaces)"
        )
        incidents = engine.run()
        print(
            f"[REPLAY] termine: {len(incidents)} incident(s) detecte(s) sur "
            "l'historique rejoue."
        )
        for incident in incidents:
            print(f"  - {incident.id}: {incident.summary_fr()}")
        return 0

    if args.command == "status":
        return _status(selection, identity, factory)

    if args.command == "record":
        from demo.record import RecordError, record_eve, record_nuar

        try:
            if args.target == "eve":
                written = record_eve(selection, args.out)
            else:
                written = [record_nuar(selection, f"{args.out}/nuar/nuar_export.json")]
        except RecordError as exc:
            print(f"[ERREUR] {exc}")
            return 2
        print("[RECORD] fixtures capturees:")
        for path in written:
            print(f"  - {path}")
        return 0

    return 2


def _status(selection, identity, factory) -> int:
    """Read-only state summary — exercises the EVE read path (Phase 1)."""
    if selection.twin == "eve":
        twin = factory.build_eve_twin(selection, identity)
        print("[STATUT] jumeau EVE-NG (lecture seule)")
    else:
        from demo.twin.sim_twin import build_default_twin

        twin = build_default_twin(seed=selection.seed)
        print("[STATUT] jumeau simule")
    print(f"  routeurs ({len(twin.get_nodes())}): {', '.join(twin.get_nodes())}")
    for link in twin.get_links():
        util = (
            f"{link['utilisation_pct']:.0f}%"
            if link.get("utilisation_pct") is not None
            else "n/d (couche telemetrie)"
        )
        state = "UP" if link.get("oper_up") else "DOWN"
        print(f"  lien {link['id']:<14} {state:<5} utilisation: {util}")
    down_sessions = [
        s for s in twin.get_bgp_sessions() if s.get("state") not in ("Established",)
    ]
    print(
        "  sessions BGP: "
        + ("toutes etablies" if not down_sessions else f"{len(down_sessions)} en panne")
    )
    if hasattr(twin, "config"):
        drift = []
        for node in twin.get_nodes():
            drift.extend(twin.config.diff(node))
        if drift:
            print("  derives de configuration:")
            for entry in drift:
                commit = entry.get("changed_at_commit")
                extra = f" (commit {commit})" if commit else ""
                print(
                    f"    - {entry['node']}: {entry['path']} "
                    f"{entry['golden']} -> {entry['running']}{extra}"
                )
        else:
            print("  configuration: aucun ecart au golden")
    flows = twin.get_flows()
    broken = [f for f in flows if f.get("status") not in ("ok", None)]
    print(
        "  joignabilite clients: "
        + ("OK" if not broken else f"{len(broken)} prefixe(s) impacte(s)")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
