"""LLM-driven ReAct reasoner over the Anthropic Messages API (optional).

Activated only when ANTHROPIC_API_KEY is set AND the user asks for
`--reasoner llm`; everything is gated so the demo never requires a key:
  - `anthropic` is imported lazily inside the constructor;
  - a missing key/package raises ReasonerUnavailable at construction;
  - any API error mid-run raises ReasonerUnavailable, and the eval runner
    falls back to the deterministic rules reasoner.

The LLM does GENUINE tool use over the exact same tool registry as the rules
reasoner: the registry's MCP-shaped manifests are passed as Anthropic
`tools=[...]`, each `tool_use` block is executed by the orchestrator through
the registry, and the result goes back as a `tool_result` block. The final
answer must come through a `conclude` tool so the output is the same
structured RootCauseReport as the rules path.
"""

from __future__ import annotations

import json
import os
from typing import Any, Generator

from demo.heartbeat.incident import Incident
from demo.rca.agent import AgentState, Conclusion, Note, Reasoner, ToolCall

DEFAULT_MODEL = "claude-opus-4-8"
MAX_LLM_TURNS = 15
MAX_TOKENS = 4000

_SYSTEM_PROMPT = """\
You are a network operations root-cause-analysis agent working on a DIGITAL
TWIN of a telecom backbone zone (8 Juniper routers, IS-IS + BGP, customer
prefixes attached at edge routers). An incident was detected by the
monitoring heartbeat; your job is to find the TRUE ROOT CAUSE, the affected
customer prefixes, and to VALIDATE the cause by counterfactual replay.

Method (follow it rigorously):
1. Pin the onset time of the symptoms (read_telemetry, change of level).
2. Sweep evidence across heterogeneous sources: diff_config on suspicious
   nodes (config drift is a frequent silent cause), get_bgp_state,
   get_isis_adjacencies, get_link_traffic, shortest_path (compare state
   "live" vs "baseline" to expose reroutes).
3. DISTINGUISH CAUSE FROM EFFECT: a congested link is an EFFECT unless
   something upstream explains it; only events/changes at or before the
   symptom onset can be causes. Congestion is NOT an injectable fault type
   and can never be a root cause on its own.
4. MANDATORY before concluding: call counterfactual_inject with your top
   hypothesis. It replays the hypothesis in a sandbox copy of the healthy
   twin and tells you whether the observed symptom signature reproduces.
   If it does not reproduce, revise the hypothesis and try again (max 3).
5. Call get_affected_clients(scope="all") to establish the impacted
   prefixes, then call `conclude` with the structured result.

Hypothesis shape for counterfactual_inject:
  {"type": "config_drift", "object": "isis_metric", "where": "<link id>",
   "params": {"node": "<router>", "new_value": <int>}}
  {"type": "config_drift", "object": "export_policy", "where": "<router>",
   "params": {"ce": "<ce name>"}}
  {"type": "link_down", "object": "link", "where": "<link id>", "params": {}}
  {"type": "session_flap", "object": "bgp_session", "where": "<session id>", "params": {}}
  {"type": "mtu_mismatch", "object": "mtu", "where": "<link id>",
   "params": {"node": "<router>", "new_value": <int>}}
  {"type": "delay_loss", "object": "link_quality", "where": "<link id>",
   "params": {"delay_ms": <float>, "loss_pct": <float>}}

Confidence rule: "elevee" only if the counterfactual reproduced AND the
evidence is temporally aligned; "moyenne" if only the counterfactual holds;
"faible" otherwise. The `detail` field of the cause must be written in
French (it is shown to French-speaking operators).
"""

_CONCLUDE_TOOL = {
    "name": "conclude",
    "description": (
        "Finalize the diagnosis. Call this exactly once, only after a "
        "counterfactual_inject validated (or repeatedly rejected) your "
        "hypotheses. This ends the investigation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "cause": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "object": {"type": "string"},
                    "where": {"type": "string"},
                    "detail": {"type": "string", "description": "French, operator-facing"},
                },
                "required": ["type", "object", "where", "detail"],
            },
            "confidence": {"type": "string", "enum": ["elevee", "moyenne", "faible"]},
            "evidence_chain": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered observations supporting the cause (French)",
            },
        },
        "required": ["cause", "confidence", "evidence_chain"],
    },
}


class ReasonerUnavailable(RuntimeError):
    """LLM reasoner cannot run (missing key/package or API failure)."""


def _incident_brief(incident: Incident) -> str:
    return json.dumps(
        {
            "incident_id": incident.id,
            "source": incident.source,
            "first_seen_tick": incident.first_seen_tick,
            "symptoms": [
                {
                    "kind": s.kind,
                    "object": s.object,
                    "onset_tick": s.onset_tick,
                    "detail_fr": s.detail_fr,
                }
                for s in incident.symptoms
            ],
            "provisional_scope": sorted(incident.provisional_scope),
        },
        ensure_ascii=False,
    )


class LlmReasoner(Reasoner):
    name = "llm"

    def __init__(self, tool_manifests: list[dict[str, Any]], model: str | None = None) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ReasonerUnavailable("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic  # lazy: optional dependency
        except ImportError as exc:
            raise ReasonerUnavailable("the 'anthropic' package is not installed") from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self._model = model or os.environ.get("RCA_LLM_MODEL", DEFAULT_MODEL)
        self._tools = list(tool_manifests) + [_CONCLUDE_TOOL]
        self._gen: Generator | None = None

    def plan_fr(self, incident: Incident) -> str:
        return (
            f"plan (raisonneur LLM {self._model}): investigation outillee de "
            "l'incident, correlation temporelle, validation contrefactuelle."
        )

    def next_action(self, state: AgentState):
        if self._gen is None:
            self._gen = self._loop(state)
            return next(self._gen)
        return self._gen.send(state.last_result)

    # ------------------------------------------------------------------
    def _loop(self, state: AgentState) -> Generator:
        anthropic = self._anthropic
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    "Incident to diagnose (JSON):\n"
                    + _incident_brief(state.incident)
                    + f"\nRouters in the zone: {', '.join(state.nodes)}."
                    " Investigate with the tools and conclude."
                ),
            }
        ]

        for _ in range(MAX_LLM_TURNS):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=MAX_TOKENS,
                    system=_SYSTEM_PROMPT,
                    tools=self._tools,
                    messages=messages,
                )
            except anthropic.APIError as exc:
                raise ReasonerUnavailable(f"Anthropic API error: {exc}") from exc
            if response.stop_reason == "refusal":
                raise ReasonerUnavailable("the model declined the request")

            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                messages.append(
                    {
                        "role": "user",
                        "content": "Continue the investigation with tools, or call `conclude`.",
                    }
                )
                continue

            results: list[dict[str, Any]] = []
            conclusion_input: dict[str, Any] | None = None
            for block in tool_uses:
                if block.name == "conclude":
                    conclusion_input = dict(block.input)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "conclusion recorded",
                        }
                    )
                    continue
                # Execute through the SAME registry as the rules reasoner.
                result = yield ToolCall(block.name, dict(block.input))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str, ensure_ascii=False),
                        "is_error": isinstance(result, dict) and "error" in result,
                    }
                )
            messages.append({"role": "user", "content": results})

            if conclusion_input is not None:
                yield Note("le raisonneur LLM a conclu; normalisation du resultat.")
                # Normalize affected clients through the deterministic tool so
                # both reasoners report the same structured impact.
                affected = yield ToolCall("get_affected_clients", {"scope": "all"})
                counterfactuals = [
                    r
                    for (tool, _args, r) in state.observations
                    if tool == "counterfactual_inject" and isinstance(r, dict)
                ]
                yield Conclusion(
                    cause=conclusion_input.get("cause", {}),
                    confidence=conclusion_input.get("confidence", "faible"),
                    evidence_chain=list(conclusion_input.get("evidence_chain", [])),
                    affected_clients=(
                        affected.get("affected", []) if isinstance(affected, dict) else []
                    ),
                    counterfactual=counterfactuals[-1] if counterfactuals else None,
                )
                return

        yield Conclusion(
            cause={
                "type": "unknown",
                "object": "unknown",
                "where": "unknown",
                "detail": "le raisonneur LLM n'a pas conclu dans le budget de tours",
            },
            confidence="faible",
            evidence_chain=["budget de tours LLM epuise sans conclusion"],
            affected_clients=[],
            counterfactual=None,
        )
