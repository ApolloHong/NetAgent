"""`--record` mode: capture fixtures from REAL endpoints for offline replay.

Recording REQUIRES a reachable real endpoint (there is nothing to capture
otherwise) — it never fabricates data. The captured files land under
`--out` (default demo/fixtures/) in exactly the shapes the fixture
transports replay, plus an inventory skeleton to complete by hand (foreign
IDs the operator must confirm are marked TODO-REVIEW in the file, never in
code paths).
"""

from __future__ import annotations

import json
from pathlib import Path

from demo.config import BackendSelection, resolve_secret
from demo.inventory.identity import DeviceRecord, IdentityMap


class RecordError(RuntimeError):
    pass


_MCP_COMMANDS = (
    "show isis adjacency | display json",
    "show bgp summary | display json",
    "show route | display json",
    "show system commit",
)


def record_eve(selection: BackendSelection, out_dir: str | Path) -> list[str]:
    """Capture EVE-NG topology/nodes + junos-mcp-server reads into fixtures."""
    from demo.twin.eve_twin import HttpEveTransport, HttpMcpTransport

    eve_cfg = selection.section("eve")
    mcp_cfg = selection.section("mcp")
    if not eve_cfg.get("base_url") or not mcp_cfg.get("url"):
        raise RecordError(
            "recording needs real endpoints: set eve.base_url and mcp.url in "
            "config.yaml (credentials via *_env variables)"
        )
    eve = HttpEveTransport(
        base_url=str(eve_cfg["base_url"]),
        username=str(eve_cfg.get("username", "admin")),
        password=resolve_secret(eve_cfg, "password") or "",
    )
    mcp = HttpMcpTransport(url=str(mcp_cfg["url"]))
    lab = str(eve_cfg.get("lab", "netops/zone1.unl"))

    out = Path(out_dir)
    (out / "eve").mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    nodes = eve.get(f"labs/{lab}/nodes")
    topology = eve.get(f"labs/{lab}/topology")
    for name, data in (("nodes", nodes), ("topology", topology)):
        path = out / "eve" / f"{name}.json"
        path.write_text(
            json.dumps({"code": 200, "status": "success", "data": data}, indent=1) + "\n"
        )
        written.append(str(path))

    routers = mcp.call("get_router_list")
    responses: dict[str, dict] = {}
    for router in routers:
        responses[router] = {
            "get_junos_config": mcp.call("get_junos_config", router_name=router)
        }
        for command in _MCP_COMMANDS:
            responses[router][command] = mcp.call(
                "execute_junos_command", router_name=router, command=command
            )
    path = out / "eve" / "mcp_responses.json"
    path.write_text(json.dumps(responses, indent=1) + "\n")
    written.append(str(path))

    # Inventory skeleton: canonical name defaults to the EVE node name minus
    # its template prefix; the operator reviews/corrects foreign IDs by hand.
    devices = [
        DeviceRecord(
            canonical=payload["name"].split("-", 1)[-1],
            eve_node_id=str(node_id),
            eve_name=payload["name"],
            mcp_name=payload["name"].split("-", 1)[-1],
            mgmt_ip=payload.get("mgmt"),
        )
        for node_id, payload in sorted(nodes.items())
    ]
    skeleton = IdentityMap(devices=devices)
    path = out / "inventory.recorded.json"
    skeleton.to_file(path)
    written.append(str(path))
    return written


def record_nuar(selection: BackendSelection, out_path: str | Path) -> str:
    """Capture a NUAR export. NUAR is internal-only: this expects an export
    endpoint or file share configured as nuar.api_url / nuar.source_path;
    without one there is nothing real to record."""
    nuar_cfg = selection.section("nuar")
    source_path = nuar_cfg.get("source_path")
    if source_path and Path(str(source_path)).exists():
        payload = Path(str(source_path)).read_text()
        Path(out_path).write_text(payload)
        return str(out_path)
    api_url = nuar_cfg.get("api_url")
    if api_url:
        import httpx  # lazy: optional [eve] extra provides httpx

        response = httpx.get(str(api_url), timeout=60)
        response.raise_for_status()
        Path(out_path).write_text(response.text)
        return str(out_path)
    raise RecordError(
        "recording NUAR needs nuar.source_path (export file share) or "
        "nuar.api_url (internal endpoint) in config.yaml; the committed "
        "fixture demo/fixtures/nuar/nuar_export.json remains available offline"
    )
