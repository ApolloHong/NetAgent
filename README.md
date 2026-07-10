# netops-demo — Detection & cause racine sur jumeau numérique réseau

An **operational intelligence layer** on top of a network **digital twin**,
built as a credible prototype for a telecom backbone operator. It does two
things:

1. **débogage automatique (heartbeat)** — a periodic patrol scans telemetry,
   control-plane events and a running-vs-golden config audit, and raises
   structured **Incidents**;
2. **cause racine pour un incident** — an agentic RCA loop gathers evidence
   across heterogeneous sources (config text, telemetry time-series, the
   topology graph, BGP/IS-IS state), ranks hypotheses while **distinguishing
   cause from effect**, and **confirms the top hypothesis by counterfactual
   replay inside a sandbox copy of the twin** before concluding.

Everything runs **offline, deterministically, with no API key** by default
(rule-based reasoner + rule judge). Operator-facing traces are printed in
**French**; all code, identifiers and comments are in English.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# The three narrated end-to-end scenarios (detect -> RCA -> counterfactual -> score):
python -m demo run --scenario config_drift
python -m demo run --scenario link_down_with_congestion
python -m demo run --scenario bgp_session_flap

# The labelled evaluation suite (8 cases, prints the summary table):
python -m demo eval
```

Options:

| Flag | Effect |
|---|---|
| `--reasoner rules\|llm` | Reasoning engine (default `rules`). `llm` needs `ANTHROPIC_API_KEY` **and** `pip install anthropic`; it does genuine tool-use over the same registry and **falls back to rules** on any missing key or error. |
| `--png [PATH]` | (run) Render the annotated topology to a PNG (needs `matplotlib`). |
| `--llm-judge` | (eval) Score root causes with an LLM judge when a key is set (objective facts stay rule-checked). |
| `--seed N` | Change the deterministic seed (default 2026). |

## What a run looks like

```
[HEARTBEAT] 10:31:40 anomalie: utilisation lien core3-core4 a 93% (seuil 85%), point de rupture a 10:31:35. Incident INC-0001 cree.
[RCA] plan: correler avec les changements de config recents, localiser sur la topologie, verifier le plan de controle...
[RCA] diff_config(core1) -> protocols.isis.interface.ge-0/0/0.metric: 10 -> 1000 (commit a 10:31:35)
[RCA] shortest_path(cust-A/24, cust-D/24, live) -> chemin (live): edge1 > core1 > core3 > core4 > edge4
[RCA] hypothese: metrique IS-IS 10 -> 1000 sur core1. Validation contrefactuelle dans le jumeau (bac a sable)...
[RCA] counterfactual_inject(core1-core2) -> en rejouant cette cause dans le bac a sable, les symptomes se REPRODUISENT.
[RESULTAT] Cause racine: metrique IS-IS 10 -> 1000 sur core1. Clients impactes: cust-A/24, cust-C/24, cust-D/24. Confiance: elevee.
```

## Architecture

```
                       ┌────────────────────────────────────────────────┐
                       │  eval/  dataset -> runner -> judges -> summary │
                       └───────────────┬────────────────────────────────┘
        inject/recover/validate        │ drives
 faults/catalog  ──────────────►  twin/SimTwin  ◄────────── read-only ─────────┐
 (link_down, mtu_mismatch,        (topology graph, SPF,                        │
  delay_loss, session_flap,        traffic, BGP/IS-IS,        heartbeat/       │
  config_drift)                    telemetry, events,         detector ──► Incident
                                   snapshot/baseline)                          │
                                       ▲    │ sandbox copies                   ▼
                                       │    └──────────► tools/diagnostics ◄── rca/agent
                                       │                 (10 tools, MCP shape)  plan->act->
                                       └── counterfactual_inject ◄───────────── observe->
                                                                                validate
                                                     reasoner_rules (default) / reasoner_llm
```

- **Detection is broad, actions are narrow**: the heartbeat only *detects and
  reports* — it holds no mutation handle. The RCA layer reads the live twin
  **read-only** and experiments only in **sandbox copies** of the healthy
  baseline. Only the fault injector (a test harness) mutates the live twin,
  and nothing here touches a real network.
- **Virtual clock + fixed seeds**: two runs of the same scenario produce
  byte-identical traces.
- **Predictor seam**: `Incident.source` is `"heartbeat"` today; a future
  capacity/traffic predictor will emit `source="forecast"` incidents (e.g. "a
  corridor will congest") and the RCA agent is source-agnostic.

### The simulated zone

8 routers (a `core1..core4` square + one edge router per core), customer
prefixes `cust-A/24..cust-D/24` attached at `edge1..edge4` over eBGP, an iBGP
mesh between edges, IS-IS metrics from per-device Junos-shaped configs, and a
static demand matrix routed by SPF (deterministic tie-break). Baseline core
loads: core1-core2 62 %, core2-core4 72 %, core1-core3 20 %, core3-core4 30 %
— so the shipped faults reroute and congest exactly as narrated (e.g. the
IS-IS metric drift pushes the A↔D flow onto core3-core4 → 92 %).

### Mock → real seams

Every interface names its real backend in its docstring; swapping the mock
for the real system is a matter of implementing an interface, not rewriting
detection/RCA logic. Rows marked **(adapter shipped)** are implemented: they
connect to the real endpoint when configured and **fall back to recorded
fixtures** ([demo/fixtures/](demo/fixtures/README.md)) when it is absent or
unreachable, so everything keeps running offline.

| Demo component (interface) | Real backend it maps to |
|---|---|
| `twin/backend.py` (`TwinBackend`), `twin/sim_twin.py` | EVE-NG lab (Juniper vMX VCP+VFP, vQFX), ZTP-bootstrapped, driven over NETCONF by **junos-mcp-server** — **(adapter shipped:** `twin/eve_twin.py` `EveNgTwin`; EVE REST `/api/auth/login`, `/api/labs/{lab}/nodes`, `/topology`; MCP tools `get_junos_config`, `execute_junos_command`, `load_and_commit_config`**)** |
| `inventory/identity.py` (`IdentityMap`) | The operator inventory: bijective canonical ↔ NUAR / EVE-NG / MCP router / config-key mapping — every real adapter normalises through it before returning data |
| `twin/telemetry.py` (`TelemetrySource`) | gNMI streaming (live) — **(adapters shipped:** `twin/nuar_telemetry.py` `NuarTelemetrySource` for NUAR 5-minute historical counters with reset/wrap/gap handling + `ReplayClock`; `engine/nautilus_engine.py` `GnmiTelemetryFeed` over **pygnmi** for the live seam**)** |
| `twin/config_store.py` (`ConfigStore`) | junos-mcp-server get-config + **real Junos commit timestamps** (`show system commit`) as the temporal-alignment signal — **(adapter shipped:** `EveConfigStore`**)**; Batfish for static audit at scale |
| `faults/` (`Fault`) | NETCONF load/commit on the lab — **(Phase 2 shipped, gated:** `EveNgTwin.commit_config` / `rollback_to_golden` behind `--allow-writes`; lab only, never production**)** |
| `heartbeat/detector.py` + `heartbeat/checks.py` | Scheduled patrol over gNMI/syslog + periodic audit; the telemetry checks are SHARED pure functions with injectable `Thresholds` (retune them on real data via `config.yaml`) |
| `rca/counterfactual.py` (`Counterfactual`) | Sandbox replay (sim, default and byte-identical to before) / **Batfish** static what-if via **pybatfish** (`fork_snapshot`, `routes`, `reachability`; routing-only coverage — congestion honestly out of scope without a traffic matrix) / EVE lab replay (interface defined, needs a clonable write-enabled lab) |
| `engine/` (`TelemetryReplayEngine`) | Builtin per-tick loop (default) / **NautilusTrader** for the telemetry replay+live lane (optional `[nautilus]` extra — see tradeoff below) |
| `tools/registry.py` (`ToolRegistry`) | **MCP** tool manifests (`{name, description, input_schema}`) exposed by junos-mcp-server, an EVE-NG server and a verification server; the path/impact tools mirror the query agent's capabilities |
| `rca/agent.py` orchestrator loop | **LangGraph** plan/act/observe/iterate graph |
| `rca/reasoner_llm.py` | Anthropic Messages API with tool use over the same MCP tools |

Known, documented gap: real devices expose link counters and RIBs, never
per-OD demands — `EveNgTwin.get_flows()` reports RIB-derived reachability
with `mbps=None`, and the `TrafficMatrixProvider` seam marks where demand
estimation (future modelling work) plugs in. Nothing fakes per-OD flows.

### Backend selection & real endpoints

Each layer is independently selectable via CLI flags or `config.yaml`
(copy [config.example.yaml](config.example.yaml); CLI wins over the file;
all defaults = sim, so offline behaviour is byte-identical to before):

```bash
python -m demo run  --scenario config_drift --counterfactual batfish
python -m demo eval                                  # the all-sim default
python -m demo replay                                # detection over NUAR history
python -m demo replay --engine nautilus              # same lane, Nautilus engine
python -m demo status --twin eve                     # EVE read path (fixtures offline)
python -m demo record --target eve --out demo/fixtures  # capture fixtures live
```

- `--twin {sim,eve}` — the scenario/eval pipeline injects faults, so
  `--twin eve` additionally requires `--allow-writes` **and** a reachable
  write-enabled lab (Phase 2); `status --twin eve` exercises the read-only
  Phase-1 path anytime (offline from fixtures).
- `--telemetry {sim,nuar}` — feeds the `replay` lane. NUAR = coarse
  5-minute historical truth: right for validating thresholds and
  false-positive behaviour on real daily/weekly patterns and as future
  predictor training data (via `Incident.source`); second-scale on-change
  signals come from gNMI on the twin, not NUAR.
- `--counterfactual {sim,batfish,eve}` — the oracle behind the
  `counterfactual_inject` tool; the structured hypothesis and the shared
  fault factory (`build_fault`) bridge all three.
- `--engine {builtin,nautilus}` — telemetry replay/live lane ONLY; silently
  falls back to builtin when `nautilus_trader` isn't installed.
- `record` captures fixtures from real endpoints (EVE topology/configs/show
  outputs into `demo/fixtures/eve/`, NUAR exports into
  `demo/fixtures/nuar/`, plus an inventory skeleton to review); recording
  requires a reachable endpoint — it never fabricates data.
- Credentials are read from environment variables named in `config.yaml`
  (e.g. `password_env: EVE_PASSWORD`); no secret is ever committed.

### The NautilusTrader tradeoff (honest note)

`--engine nautilus` swaps the telemetry lane's per-tick loop for
NautilusTrader: Parquet-catalog persistence, deterministic `ts_init`-ordered
replay through a `BacktestEngine`, and the SAME `HeartbeatActor` running in
backtest and live (gNMI feed → sandbox = real-time replay). The detection
logic is **not** duplicated — both engines call the same pure functions in
`heartbeat/checks.py`, and `tests/test_engine_parity.py` requires identical
incidents from both. Costs: a heavy **LGPL-3.0** dependency (isolated in the
optional `[nautilus]` extra so the core stays permissively licensed), and a
venue/instrument-oriented API used here in its low-level custom-data-only
mode (venues are optional there — doc-verified). One documented limitation:
the full `LiveDataClient` wrapper for gNMI is left as deployment wiring
(its factory surface was not doc-verified); the feed, the data type and the
actor it would glue together are shipped and tested. This engine is used
for this one lane and nothing else.

### Tests

```bash
python -m unittest discover tests
```

Covers: regression (golden byte-for-byte scenario traces + 7/7 eval on the
all-sim defaults), identity bijectivity/round-trips, NUAR reset/wrap/gap
handling and replay-clock gating, EVE fixture integration (read path, real
commit timestamps, gated Phase-2 writes), counterfactual oracles (sim
behaviour identical, Batfish discriminates hypotheses and reports dynamic
ones unsupported, EVE gated), replay-lane detection over the recorded
anomaly, and builtin-vs-Nautilus parity (skips cleanly without the extra).
All of it runs offline from committed fixtures — CI needs no infra.

### Evaluation philosophy

The agent is judged on whether it **gets the job done** under controlled
failures — did it detect, did it name the true root cause, did it identify
the right affected clients — not on how fluently it explains itself. Hence:

- expected root causes are **structured** (`{type, object, where}`) and the
  default judge is an exact rule match (an LLM judge is optional);
- the affected-client set is always checked rule-based (objective fact);
- each case validates injection took effect, and that recovery returns the
  twin to a provably clean state;
- a fault-free case (`clean-01`) measures detection **precision**.

`python -m demo eval` prints: detection rate, mean detection latency (ticks),
precision/recall, diagnosis pass rate (cause + clients), mean steps, mean RCA
latency, recovery/validation success, error rate, and agent-vs-human time.

## Package layout

```
demo/
├── __main__.py        CLI (run / eval / replay / status / record)
├── clock.py           VirtualClock (sim) + ReplayClock (real history)
├── config.py          backend selection + config.yaml loader (secrets via env)
├── factory.py         builds the selected backends, injects into the pipeline
├── record.py          --record: capture fixtures from real endpoints
├── inventory/         canonical id space + bijective foreign-id mappers (A)
├── twin/              SimTwin (default) · EveNgTwin (B) · NuarTelemetrySource (C)
├── faults/            fault catalog, inject -> recover -> validate
├── heartbeat/         detector + SHARED pure checks (injectable Thresholds)
├── tools/             tool registry (MCP shape) + 10 diagnostic tools
├── rca/               orchestrator, reasoners, analysis + Counterfactual oracles (D)
├── engine/            telemetry replay/live lane: builtin + Nautilus (E)
├── eval/              labelled dataset, judges, runner, summary table
├── fixtures/          recorded captures replayed by every adapter offline
└── viz.py             optional annotated topology PNG
tests/                 regression + per-adapter fixture tests + parity check
```
