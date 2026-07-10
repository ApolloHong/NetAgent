"""Tool registry: named tools with JSON schemas, callable by both reasoners.

REAL BACKEND: each entry maps 1:1 to an MCP tool exposed by one of the
project's MCP servers — junos-mcp-server for device reads (config, BGP,
facts, show commands), an EVE-NG server for lab state, and a verification
server for path/impact queries (the query agent's capabilities). The
`{name, description, input_schema}` triple below is exactly the MCP tool
manifest shape, and is also directly usable as an Anthropic Messages API
`tools=[...]` entry — which is how the LLM reasoner consumes this registry.

The rule-based reasoner calls the same registry through `call()`; both
reasoners therefore see identical capabilities and identical results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class ToolError(Exception):
    """Raised on unknown tool or invalid arguments."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]

    def manifest(self) -> dict[str, Any]:
        """MCP / Anthropic tool-use manifest entry."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        fn: Callable[..., Any],
    ) -> None:
        self._tools[name] = ToolSpec(name, description, input_schema, fn)

    def manifests(self) -> list[dict[str, Any]]:
        return [self._tools[n].manifest() for n in sorted(self._tools)]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        args = args or {}
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(f"unknown tool: {name}")
        required = spec.input_schema.get("required", [])
        missing = [r for r in required if r not in args]
        if missing:
            raise ToolError(f"{name}: missing required argument(s): {missing}")
        allowed = set(spec.input_schema.get("properties", {}))
        unknown = [k for k in args if k not in allowed]
        if unknown:
            raise ToolError(f"{name}: unknown argument(s): {unknown}")
        return spec.fn(**args)
