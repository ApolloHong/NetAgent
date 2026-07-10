"""netops-demo: operational intelligence layer on top of a network digital twin.

Two capabilities:
  1. Automatic incident detection (heartbeat patrol over telemetry + config audit).
  2. Root-cause analysis of a detected incident, with counterfactual replay
     inside a sandbox copy of the twin.

The twin here is a deterministic Python simulation, but every component sits
behind an interface whose real-world backend is named in its docstring
(EVE-NG, junos-mcp-server over NETCONF, gNMI, Batfish, MCP, LangGraph), so
swapping the mock for the real thing is a matter of implementing an interface.
"""

__version__ = "0.1.0"
