"""Backend selection + config.yaml loader.

Selection precedence: CLI flags > config file > defaults (all-sim, offline).
The config file may be YAML (needs the optional pyyaml; a minimal built-in
parser covers the flat two-level shape of config.example.yaml) or JSON
(stdlib). CREDENTIALS ARE NEVER STORED IN THE FILE: fields ending in `_env`
name the environment variable that holds the secret (e.g. `password_env:
EVE_PASSWORD`), and `${VAR}` values are expanded from the environment.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from demo.heartbeat.checks import Thresholds

DEFAULT_CONFIG_NAMES = ("config.yaml", "config.yml", "config.json")
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@dataclass
class BackendSelection:
    """One resolved choice per swappable layer (defaults = current sim)."""

    twin: str = "sim"  # sim | eve
    telemetry: str = "sim"  # sim | nuar
    counterfactual: str = "sim"  # sim | batfish | eve
    engine: str = "builtin"  # builtin | nautilus (telemetry lane only)
    reasoner: str = "rules"  # rules | llm (unchanged)
    allow_writes: bool = False
    seed: int = 2026
    raw: dict[str, Any] = field(default_factory=dict)  # full config payload

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name, {})
        return value if isinstance(value, dict) else {}


def _coerce(text: str) -> Any:
    text = text.strip().strip('"').strip("'")
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def _parse_minimal_yaml(text: str) -> dict[str, Any]:
    """Fallback parser for the flat two-level `key: value` shape used by
    config.example.yaml (no lists, no multiline). Install pyyaml for full
    YAML support; JSON always works with the stdlib."""
    root: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        indented = line.startswith((" ", "\t"))
        key = key.strip()
        if not indented:
            if value.strip():
                root[key] = _coerce(value)
                current = None
            else:
                current = root.setdefault(key, {})
        elif current is not None:
            current[key] = _coerce(value)
    return root


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, str):
        return re.sub(
            r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m.group(1), ""), value
        )
    return value


def load_config(path: str | None = None) -> dict[str, Any]:
    """Load the config file (explicit path, or the first default name found
    in the current directory). Returns {} when there is none — the offline
    all-sim defaults need no file at all."""
    candidate: Path | None = Path(path) if path else None
    if candidate is None:
        for name in DEFAULT_CONFIG_NAMES:
            if Path(name).exists():
                candidate = Path(name)
                break
    if candidate is None:
        return {}
    if not candidate.exists():
        raise FileNotFoundError(f"config file not found: {candidate}")
    text = candidate.read_text()
    if candidate.suffix == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml  # lazy: optional dependency

            payload = yaml.safe_load(text) or {}
        except ImportError:
            payload = _parse_minimal_yaml(text)
    return _expand_env(payload)


def resolve_secret(section: dict[str, Any], key: str) -> str | None:
    """`password_env: EVE_PASSWORD` -> os.environ['EVE_PASSWORD'];
    a literal `password:` is honoured but discouraged (never commit one)."""
    env_name = section.get(f"{key}_env")
    if env_name:
        return os.environ.get(str(env_name))
    literal = section.get(key)
    return str(literal) if literal is not None else None


def make_selection(args: Any, config: dict[str, Any]) -> BackendSelection:
    """Merge CLI args over the config file over defaults."""
    def pick(name: str, default: Any) -> Any:
        cli = getattr(args, name, None)
        if cli is not None:
            return cli
        return config.get(name, default)

    return BackendSelection(
        twin=pick("twin", "sim"),
        telemetry=pick("telemetry", "sim"),
        counterfactual=pick("counterfactual", "sim"),
        engine=pick("engine", "builtin"),
        reasoner=pick("reasoner", "rules"),
        allow_writes=bool(pick("allow_writes", False)),
        seed=int(pick("seed", 2026)),
        raw=config,
    )


def thresholds_from(config: dict[str, Any]) -> Thresholds:
    """Detection thresholds, overridable from config (real data needs
    retuning — see demo/heartbeat/checks.py)."""
    section = config.get("thresholds", {})
    if not isinstance(section, dict) or not section:
        return Thresholds()
    defaults = Thresholds()
    return Thresholds(
        congestion_pct=float(section.get("congestion_pct", defaults.congestion_pct)),
        error_rate=float(section.get("error_rate", defaults.error_rate)),
        latency_ms=float(section.get("latency_ms", defaults.latency_ms)),
        drop_shift_pct=float(section.get("drop_shift_pct", defaults.drop_shift_pct)),
        change_point_score=float(
            section.get("change_point_score", defaults.change_point_score)
        ),
        telemetry_window=int(section.get("telemetry_window", defaults.telemetry_window)),
        audit_every=int(section.get("audit_every", defaults.audit_every)),
    )
