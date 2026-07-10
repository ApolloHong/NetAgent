# Recorded fixtures

Every real-backend adapter falls back to these committed captures when its
endpoint or credentials are absent, so the whole pipeline (and CI) runs
offline. `python -m demo record --target {eve,nuar}` refreshes them from
live endpoints.

| File | Replayed by | Shape |
|---|---|---|
| `inventory.json` | `IdentityMap.from_file` | canonical <-> NUAR/EVE/MCP id mapping (item A) |
| `nuar/nuar_export.json` | `NuarTelemetrySource` | NUAR export schema: 5-min octet/error counters, 4 interfaces x 6 h, containing one real-looking congestion step (core3-core4), one counter reset and one recording gap. NUAR is internal-only, so this capture is synthetic-but-realistic; the schema is the documented contract. |
| `eve/nodes.json`, `eve/topology.json` | `FixtureEveTransport` | EVE-NG REST envelopes (`{code,status,data}`) |
| `eve/mcp_responses.json` | `FixtureMcpTransport` | junos-mcp-server tool payloads per router: `get_junos_config` (display-set), `show isis adjacency / bgp summary / route | display json`, `show system commit` (core1 carries a real drift + netconf commit timestamp) |
| `eve/golden/*.set` | `EveConfigStore` | golden reference configs (display-set) |
| `batfish/answers.json` | `FixtureBatfishBackend` | captured what-if answers keyed by canonical hypothesis id |
