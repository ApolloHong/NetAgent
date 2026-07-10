"""Evaluation runner: inject -> detect -> RCA -> judge -> recover -> validate.

Also the shared execution path for `python -m demo run --scenario ...`
(scenarios are labelled cases run verbosely with the French trace).

Per case it records: detection (yes/no, latency in ticks, false positives),
diagnosis (judge verdict on the cause + rule check on the affected-client
set, steps, tool calls, wall latency), and cleanup (fault recovered and
validated, twin back to a clean state).
"""

from __future__ import annotations

from dataclasses import dataclass

from demo.clock import TICK_SECONDS
from demo.eval.dataset import CASES
from demo.eval.judge import judge_affected_clients, judge_root_cause
from demo.faults.catalog import build_fault
from demo.heartbeat.detector import HeartbeatDetector
from demo.heartbeat.incident import Incident
from demo.rca.agent import RcaAgent, Reasoner, RootCauseReport
from demo.rca.reasoner_rules import RulesReasoner
from demo.tools.diagnostics import build_toolset
from demo.twin.sim_twin import build_default_twin

QUIET_TICKS = 3  # pre-injection patrol window (false-positive check)
DETECTION_TIMEOUT_TICKS = 12
SETTLE_TICKS = 3  # extra patrols after detection to enrich the incident
CLEAN_TICKS_AFTER_DETECTION = 12  # clean case: total watched ticks


@dataclass
class CaseResult:
    case_id: str
    difficulty: str
    injected_ok: bool = True
    detected: bool = False
    detection_latency_ticks: int | None = None
    false_positives: int = 0
    diagnosis_pass: bool = False
    clients_pass: bool = False
    judge_why: str = ""
    steps: int = 0
    tool_calls: int = 0
    rca_wall_s: float = 0.0
    recover_ok: bool = True
    clean_ok: bool = True
    error: str = ""
    report: RootCauseReport | None = None
    human_baseline_s: int = 0
    agent_seconds: float | None = None
    expected_detection: bool = True
    reasoner_used: str = "-"


def make_reasoner(name: str, registry) -> tuple[Reasoner, str]:
    """Build the requested reasoner; degrade cleanly to rules if LLM is off."""
    if name == "llm":
        from demo.rca.reasoner_llm import LlmReasoner, ReasonerUnavailable

        try:
            return LlmReasoner(registry.manifests()), "llm"
        except ReasonerUnavailable as exc:
            print(f"[INFO] raisonneur LLM indisponible ({exc}); repli sur les regles.")
    return RulesReasoner(), "rules"


def _run_rca(
    twin, incident: Incident, registry, reasoner_name: str, verbose: bool
) -> tuple[RootCauseReport, str]:
    reasoner, used = make_reasoner(reasoner_name, registry)
    agent = RcaAgent(twin, incident, reasoner, registry, verbose=verbose)
    if used == "llm":
        try:
            return agent.run(), used
        except Exception as exc:  # any LLM failure -> deterministic fallback
            print(f"[INFO] echec du raisonneur LLM ({exc}); repli sur les regles.")
    reasoner = RulesReasoner()
    agent = RcaAgent(twin, incident, reasoner, registry, verbose=verbose)
    return agent.run(), "rules"


def run_case(
    case: dict,
    seed: int = 2026,
    reasoner: str = "rules",
    verbose: bool = False,
    use_llm_judge: bool = False,
    png_path: str | None = None,
    counterfactual_factory=None,  # (twin, incident) -> Counterfactual; None = sim
) -> CaseResult:
    result = CaseResult(
        case_id=case["case_id"],
        difficulty=case["difficulty"],
        human_baseline_s=case["human_baseline_seconds"],
        expected_detection=case["fault"] is not None,
    )
    try:
        _run_case_inner(
            case, seed, reasoner, verbose, use_llm_judge, png_path, result,
            counterfactual_factory,
        )
    except Exception as exc:  # count as an error, keep the eval going
        result.error = f"{type(exc).__name__}: {exc}"
        if verbose:
            raise
    return result


def _run_case_inner(
    case: dict,
    seed: int,
    reasoner: str,
    verbose: bool,
    use_llm_judge: bool,
    png_path: str | None,
    result: CaseResult,
    counterfactual_factory=None,
) -> None:
    twin = build_default_twin(seed=seed)
    detector = HeartbeatDetector(twin, verbose=verbose)

    if verbose:
        clock = twin.clock
        print(
            f"[JUMEAU] {clock.time_str()} zone emulee prete: 8 routeurs, "
            "4 prefixes clients, base de reference capturee."
        )
        print(f"[SCENARIO] {case['case_id']}: {case['symptom']}")

    # -- quiet pre-injection window: any incident here is a false positive
    for _ in range(QUIET_TICKS):
        twin.tick()
        if detector.patrol() is not None:
            result.false_positives += 1

    fault = build_fault(case["fault"]) if case["fault"] else None
    incident: Incident | None = None

    if fault is not None:
        fault.inject(twin)
        result.injected_ok = fault.validate(twin, expect_active=True)
        inject_tick = twin.clock.now()
        if verbose:
            print(
                f"[PANNE] {twin.clock.time_str()} injection de {fault.fault_id} "
                f"(validee: {'oui' if result.injected_ok else 'NON'})"
            )

        # -- detection window
        for _ in range(DETECTION_TIMEOUT_TICKS):
            twin.tick()
            new_incident = detector.patrol()
            if new_incident is not None:
                incident = new_incident
                break
        if incident is not None:
            result.detected = True
            result.detection_latency_ticks = incident.first_seen_tick - inject_tick
            for _ in range(SETTLE_TICKS):  # enrich signature / attach symptoms
                twin.tick()
                if detector.patrol() is not None:
                    result.false_positives += 1
    else:
        # clean case: watch for spurious incidents only
        for _ in range(CLEAN_TICKS_AFTER_DETECTION):
            twin.tick()
            if detector.patrol() is not None:
                result.false_positives += 1

    # -- RCA on the detected incident
    if incident is not None:
        oracle = (
            counterfactual_factory(twin, incident) if counterfactual_factory else None
        )
        registry = build_toolset(twin, incident, counterfactual=oracle)
        report, used = _run_rca(twin, incident, registry, reasoner, verbose)
        result.report = report
        result.reasoner_used = used
        result.steps = report.steps
        result.tool_calls = report.tool_calls
        result.rca_wall_s = report.wall_seconds
        if verbose:
            report.print_fr()

        verdict = judge_root_cause(
            report.cause, case["expected_root_cause"], use_llm=use_llm_judge
        )
        clients = judge_affected_clients(
            report.affected_clients, case["expected_affected_clients"]
        )
        result.diagnosis_pass = verdict["match"]
        result.clients_pass = clients["match"]
        result.judge_why = verdict["why"] + (
            "" if clients["match"] else f" ; clients: {clients['why']}"
        )
        result.agent_seconds = (
            (result.detection_latency_ticks or 0) * TICK_SECONDS + report.wall_seconds
        )
        if verbose:
            print(
                f"[EVALUATION] cause racine: {'PASS' if verdict['match'] else 'FAIL'} "
                f"({verdict['why']}) ; clients impactes: "
                f"{'PASS' if clients['match'] else 'FAIL'} ({clients['why']})"
            )

        if png_path:
            try:
                from demo.viz import render_topology

                render_topology(twin, incident, report, png_path)
                if verbose:
                    print(f"[VISU] topologie annotee enregistree dans {png_path}")
            except ImportError:
                print("[VISU] matplotlib absent: PNG non genere (pip install matplotlib)")

    # -- recover and validate the twin is clean again
    if fault is not None:
        fault.recover(twin)
        result.recover_ok = fault.validate(twin, expect_active=False)
        twin.tick()
        twin.tick()
        result.clean_ok = twin.is_clean()
        detector.close()
        if verbose:
            print(
                f"[RECUPERATION] {twin.clock.time_str()} panne retiree "
                f"(validee: {'oui' if result.recover_ok else 'NON'}), "
                f"jumeau redevenu propre: {'oui' if result.clean_ok else 'NON'}."
            )


# ---------------------------------------------------------------------------
# Aggregate eval
# ---------------------------------------------------------------------------
def _fmt(value, width: int, decimals: int | None = None) -> str:
    if value is None:
        return "-".rjust(width)
    if decimals is not None:
        return f"{value:.{decimals}f}".rjust(width)
    return str(value).rjust(width)


def run_eval(
    reasoner: str = "rules",
    seed: int = 2026,
    use_llm_judge: bool = False,
    verbose: bool = False,
    counterfactual_factory=None,
) -> list[CaseResult]:
    results = [
        run_case(
            case,
            seed=seed,
            reasoner=reasoner,
            verbose=verbose,
            use_llm_judge=use_llm_judge,
            counterfactual_factory=counterfactual_factory,
        )
        for case in CASES
    ]
    print_summary(results)
    return results


def print_summary(results: list[CaseResult]) -> None:
    fault_cases = [r for r in results if r.expected_detection]
    diagnosed = [r for r in fault_cases if r.report is not None]

    print()
    print("=" * 100)
    print("RESUME DE L'EVALUATION")
    print("=" * 100)
    header = (
        f"{'cas':<26}{'diff.':<8}{'detecte':<9}{'lat.':<6}{'diagnostic':<12}"
        f"{'clients':<9}{'etapes':<8}{'RCA (s)':<9}{'recup.':<8}{'erreur':<7}"
    )
    print(header)
    print("-" * 100)
    for r in results:
        detected = ("oui" if r.detected else "NON") if r.expected_detection else (
            "faux+!" if r.false_positives else "calme"
        )
        diagnosis = (
            ("PASS" if r.diagnosis_pass else "FAIL") if r.report else "-"
        )
        clients = ("PASS" if r.clients_pass else "FAIL") if r.report else "-"
        recup = "ok" if (r.recover_ok and r.clean_ok) else "NON"
        latency = f"{r.detection_latency_ticks}t" if r.detection_latency_ticks is not None else "-"
        print(
            f"{r.case_id:<26}{r.difficulty:<8}{detected:<9}{latency:<6}{diagnosis:<12}"
            f"{clients:<9}{r.steps:<8}{_fmt(r.rca_wall_s, 7, 3):<9}{recup:<8}"
            f"{'oui' if r.error else '-':<7}"
        )
        if r.error:
            print(f"    !! {r.error}")
        elif r.report and not (r.diagnosis_pass and r.clients_pass):
            print(f"    !! {r.judge_why}")

    n_fault = len(fault_cases)
    detected = [r for r in fault_cases if r.detected]
    true_pos = len(detected)
    false_pos = sum(r.false_positives for r in results)
    precision = true_pos / (true_pos + false_pos) if (true_pos + false_pos) else 1.0
    recall = true_pos / n_fault if n_fault else 1.0
    latencies = [r.detection_latency_ticks for r in detected]
    mean_latency = sum(latencies) / len(latencies) if latencies else None

    diag_pass = sum(1 for r in diagnosed if r.diagnosis_pass)
    clients_pass = sum(1 for r in diagnosed if r.clients_pass)
    mean_steps = sum(r.steps for r in diagnosed) / len(diagnosed) if diagnosed else None
    mean_wall = sum(r.rca_wall_s for r in diagnosed) / len(diagnosed) if diagnosed else None
    recovery_ok = sum(1 for r in fault_cases if r.recover_ok and r.clean_ok)
    errors = sum(1 for r in results if r.error)

    speedups = [
        r.human_baseline_s / r.agent_seconds
        for r in diagnosed
        if r.agent_seconds and r.human_baseline_s
    ]
    mean_human = (
        sum(r.human_baseline_s for r in diagnosed) / len(diagnosed) if diagnosed else 0
    )
    mean_agent = (
        sum(r.agent_seconds for r in diagnosed if r.agent_seconds) / len(diagnosed)
        if diagnosed
        else 0
    )

    print("-" * 100)
    print(
        f"Detection: {true_pos}/{n_fault} ({recall:.0%}) ; "
        f"latence moyenne: "
        + (f"{mean_latency:.1f} tick(s)" if mean_latency is not None else "-")
        + f" ; precision: {precision:.0%} ; rappel: {recall:.0%} ; "
        f"faux positifs: {false_pos}"
    )
    print(
        f"Diagnostic: {diag_pass}/{len(diagnosed)} PASS cause racine ; "
        f"{clients_pass}/{len(diagnosed)} PASS clients impactes ; "
        f"etapes moyennes: "
        + (f"{mean_steps:.1f}" if mean_steps is not None else "-")
        + " ; latence RCA moyenne: "
        + (f"{mean_wall:.3f} s" if mean_wall is not None else "-")
    )
    print(
        f"Recuperation/validation: {recovery_ok}/{n_fault} ; "
        f"taux d'erreur: {errors}/{len(results)}"
    )
    if speedups:
        print(
            f"Agent vs humain: {mean_agent:.1f} s (agent, detection virtuelle + RCA) "
            f"contre {mean_human:.0f} s (baseline humaine) ; "
            f"acceleration moyenne: x{sum(speedups) / len(speedups):.0f}"
        )
    print("=" * 100)
