"""Root-cause judge: scores a diagnosed cause against the expected label.

Two judges behind one function:
  - RULE JUDGE (default, always available): exact structured match on the
    {type, object, where} triple. The expected cause is structured precisely
    so no NLP is needed to score it — this reflects the evaluation
    philosophy: judge whether the agent got the JOB done, not whether it
    explains itself fluently.
  - LLM JUDGE (optional): a single Anthropic Messages call comparing the two
    structured causes for semantic equivalence; used only when
    ANTHROPIC_API_KEY is set and the caller asked for it, and it falls back
    to the rule judge on any error.

Objective facts (the affected-client set) are ALWAYS checked rule-based,
regardless of the judge used for the cause.
"""

from __future__ import annotations

import json
import os
from typing import Any

_JUDGE_MODEL = "claude-opus-4-8"


def _norm_where(where: str) -> str:
    """Normalise a location id (link endpoints are order-insensitive)."""
    where = str(where).strip()
    if "-" in where and "~" not in where and ":" not in where:
        parts = where.split("-")
        if len(parts) == 2:
            return "-".join(sorted(parts))
    return where


def rule_judge(diagnosed: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "type": str(diagnosed.get("type", "")).strip() == str(expected.get("type", "")).strip(),
        "object": str(diagnosed.get("object", "")).strip()
        == str(expected.get("object", "")).strip(),
        "where": _norm_where(diagnosed.get("where", "")) == _norm_where(expected.get("where", "")),
    }
    match = all(checks.values())
    bad = [k for k, ok in checks.items() if not ok]
    why = (
        "correspondance exacte (type, object, where)"
        if match
        else "divergence sur: " + ", ".join(bad)
    )
    return {"match": match, "method": "rule", "why": why}


def llm_judge(diagnosed: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """One-shot LLM comparison; raises on any failure (caller falls back)."""
    import anthropic  # lazy: optional dependency

    client = anthropic.Anthropic()
    prompt = (
        "You are grading a network root-cause-analysis agent. Compare the "
        "DIAGNOSED root cause against the EXPECTED root cause and decide "
        "whether they designate the same fault (same fault type, same "
        "faulty object, same location — wording differences do not matter).\n"
        f"DIAGNOSED: {json.dumps(diagnosed, ensure_ascii=False)}\n"
        f"EXPECTED: {json.dumps(expected, ensure_ascii=False)}\n"
        'Answer with STRICT JSON only: {"match": true|false, "why": "<short reason>"}'
    )
    response = client.messages.create(
        model=os.environ.get("RCA_JUDGE_MODEL", _JUDGE_MODEL),
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    verdict = json.loads(text)
    return {"match": bool(verdict["match"]), "method": "llm", "why": str(verdict.get("why", ""))}


def judge_root_cause(
    diagnosed: dict[str, Any], expected: dict[str, Any], use_llm: bool = False
) -> dict[str, Any]:
    if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return llm_judge(diagnosed, expected)
        except Exception as exc:  # any failure -> deterministic fallback
            result = rule_judge(diagnosed, expected)
            result["why"] += f" (juge LLM indisponible: {exc})"
            return result
    return rule_judge(diagnosed, expected)


def judge_affected_clients(
    reported: list[dict[str, Any]], expected_prefixes: list[str]
) -> dict[str, Any]:
    """Objective, always rule-based: exact set match on impacted prefixes."""
    got = {c["prefix"] for c in reported}
    want = set(expected_prefixes)
    match = got == want
    why = (
        "ensemble de clients exact"
        if match
        else f"attendu {sorted(want)}, obtenu {sorted(got)}"
    )
    return {"match": match, "method": "rule", "why": why}
