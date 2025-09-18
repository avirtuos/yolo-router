# Prompt History

I'm trying something a bit different in this project, namely spec driven development instead of my usual "Vibe" approach. I figured it might be interesting to record the prompts and resulting plan for key milestones.


### Prompt 1: Getting Started


Prompt: "Create a plan for implementing the application described in the design.md file within our project."

Plan:

```
Implementation plan for "yolo-router" simulation

1. Goals and scope

- Build a discrete-event simulation that can run two load balancing architectures: round-robin and least-connections.

- Support configurable traffic characteristics and target fleet parameters through a JSON file.

- Include a simulated scaling service with delays for scale up/down operations.

- Produce a final report containing:

  - Histogram of number of retries per request
  - Percentiles p100, p99, p90, p80, p50 for: target concurrent requests, request duration, request end-to-end latency, target CPU usage
  - Percentiles for concurrent number of target hosts in the fleet over the run

2. Tech stack and key libraries

- Python 3.11+
- SimPy for event-driven simulation and simulated time
- NumPy for distributions, RNG, and percentile calculations
- SciPy (optional) for additional distributions; can fallback to NumPy where possible
- (Optional) Matplotlib for plots if we decide to output charts; initial implementation can output JSON/Markdown/CSV tables only

3. Project structure (proposed)

- pyproject.toml or requirements.txt (dependencies)

- yolo_router/

  - __init__.py

  - config.py (dataclasses + JSON loader + validation)

  - rng.py (seeded RNG helpers)

  - simulation/

    - env.py (SimPy Environment wrapper + clocks, sampling helpers)

    - request.py (Request model, generation logic)

    - target.py (Target actor: accept/reject, service process, metrics hooks)

    - scaling.py (ScalingService simulation with delays)

    - topology.py (Registry of active targets, event bus for updates)

    - metrics.py (MetricsCollector and metric series)

    - sampler.py (Time-series sampling of fleet size, CPU usage)

    - load_balancers/

      - base.py (abstract LBHost base: interface for assign/complete hooks)
      - round_robin.py
      - least_conns.py

  - reporting/

    - aggregators.py (percentiles, histograms, summary calculations)
    - writers.py (json, csv, markdown writers; optional matplotlib)
    - report.py (compose final report from metric artifacts)

  - cli.py (argparse entry point to run simulation with a config)

- configs/

  - example_round_robin.json
  - example_least_conns.json

- tests/
  - unit tests for selection logic, scaling triggers, metrics accuracy, RNG determinism

- design.md, README.md

4. Configuration schema (JSON)

- simulation:

  - seed: int
  - duration_ms: int
  - warmup_ms: int (optional; exclude from reporting if needed)
  - sampling_interval_ms: int (for fleet size/CPU sampling)

- traffic:

  - arrival:

    - distribution: "poisson" | "exponential" | "normal" | "deterministic"
    - rate_per_sec or params: {mean, stddev, lambda, interval_ms}

  - request_duration_ms:

    - mode: "distribution" | "percentiles"
    - distribution: "normal" | "lognormal" | "exponential" (if mode=distribution)
      - params: {mean, stddev} or appropriate
    - percentiles: {p50, p80, p90, p99, p100} (if mode=percentiles; we fit/approximate)

  - cpu_demand:

    - mode: "distribution" | "percentiles"
    - distribution and/or percentiles as above; output unit: arbitrary CPU units

- load_balancer:

  - architecture: "round_robin" | "least_conns"

  - hosts: int

  - rr:

    - retry_limit: int
    - sync_delay_ms: int (propagation delay of target list changes to other hosts)
    - idle_scale_down_threshold_requests_5m: int

  - lc:

    - shared_map_lock_latency_ms: int (0 by default; can simulate small overhead)
    - idle_scale_down_threshold_outstanding_5m: int

- targets:

  - initial_count: int
  - max_concurrency_per_target: int
  - cpu_capacity_per_target: float (CPU units)

- scaling:

  - min_targets: int
  - max_targets: int
  - scale_up_delay_ms: int
  - scale_down_delay_ms: int

- reporting:

  - output_dir: string
  - writers: ["json", "markdown", "csv"] (set)
  - include_plots: bool (optional)

5. Simulation flow

- Initialize seeded RNG and SimPy environment; create initial target fleet and LB hosts.

- Start processes:

  - Request generator: emits requests at intervals derived from arrival distribution; assign each request to a randomly-chosen LB host (uniform).

  - LB host processes: handle request assignment according to architecture.

  - Target processes: each accepted request runs for its computed service duration; update metrics on start/end.

  - ScalingService: responds to scale_up/scale_down calls with configured delays and triggers topology updates on completion.

  - Sampler: at fixed sampling_interval_ms, record:

    - fleet size (# active targets)
    - per-target instantaneous CPU utilization = sum(active_request.cpu_demand) / cpu_capacity
    - per-target concurrent requests

- End of run: stop generators, let in-flight requests finish if desired; aggregate metrics; produce report.

6. Core component designs

- Request model (`Request`)

  - id, arrival_time, assigned_lb_host_id, chosen_target_id (set on success)
  - duration_ms, cpu_demand
  - retries_attempted
  - start_service_time, end_time
  - latency_ms = end_time - arrival_time
  - service_duration_ms = end_time - start_service_time

- Target (`Target`)

  - id, max_concurrency, cpu_capacity

  - state: current_concurrency, active_requests list

  - accept(request):

    - if current_concurrency < max_concurrency: start service process; return True
    - else: return False

  - service process:

    - record start_service_time
    - optionally compute slowdown factor if CPU utilization > 1.0 (planned as phase 2; phase 1: use provided request duration as-is)
    - yield env.timeout(effective_duration_ms)
    - record end_time; decrement concurrency; invoke completion hooks for LB to update shared/local state

  - metrics hooks: track concurrent samples, durations, latencies, CPU usage snapshots

- Topology/Registry (`Topology`)

  - authoritative list of active targets (objects)
  - subscription mechanism for LB hosts to receive updates
  - helper to add/remove targets and broadcast updates (with per-architecture propagation behavior)

- Scaling service (`ScalingService`)

  - scale_up(n):

    - yield env.timeout(scale_up_delay_ms)
    - create n targets; add to Topology; return new IDs

  - scale_down(target_id):

    - yield env.timeout(scale_down_delay_ms)
    - remove from Topology

- Metrics (`MetricsCollector`)

  - request-level lists: retries histogram data, latencies, service durations
  - per-target time series: concurrent_requests samples, CPU utilization samples
  - fleet time series: active_target_count samples
  - percentiles computed via NumPy on sample arrays

- Distributions (`rng`/`stats`)

  - Utility to sample per config: normal/lognormal/exponential/deterministic
  - For percentiles mode, convert p50/p80/p90/p99 to approximate distribution parameters (e.g., fit lognormal) or piecewise sampling; start with lognormal fit using percentile-to-params method; fallback to empirical sampling table if provided

- LB base (`LBHostBase`)

  - methods:

    - assign(request)
    - on_request_complete(target_id)

  - host_id, references to topology, scaling service, metrics

- Round-robin (`RoundRobinLBHost`)

  - local target_list with index

  - on topology update:

    - if event initiated by this host (scale up/down), update immediately
    - other hosts receive update after sync_delay_ms via scheduled event

  - assign(request):

    - for up to retry_limit:

      - pick next target (round-robin), try accept()
      - on accept: record retries and exit
      - on reject: continue

    - if all rejected:
      - call scale_up(1); upon completion, immediately know about new target; send request there

  - scale-down policy:
    - periodic task scans targets; if a target handled fewer than threshold requests in past 5 min, initiate scale down

- Least-conns (`LeastConnsLBHost`)

  - shared map: target_id -> outstanding_count; protected by a simple SimPy Resource mutex

  - assign(request):

    - acquire mutex; choose target with minimal outstanding_count where count < max_concurrency; increment chosen count; release mutex
    - if no target available:
      - call scale_up(1); upon completion: add new target to shared map with outstanding=1 and route request there

  - on_request_complete(target_id):
    - acquire mutex; decrement outstanding_count; release mutex

  - scale-down policy:
    - periodic task scans shared map; targets with fewer than threshold outstanding over last 5 minutes -> initiate scale down and remove from shared after scaler completes

7. Reporting

- Compute:

  - retries histogram: counts of requests that needed 1, 2, 3, ... attempts

  - percentiles (p100, p99, p90, p80, p50) for:

    - per-target concurrent requests (sample-based across time; also overall distribution across all targets)
    - request durations (from target-run time)
    - end-to-end latencies (from LB arrival to completion)
    - CPU utilization per target (samples across time)

  - fleet size percentiles (from sampled active_target_count)

- Writers:

  - JSON: structured metrics and percentiles
  - Markdown: human-readable summary with tables
  - CSV: raw series if needed (optional, behind config)

- Optional plots:

  - Histogram for retries
  - Time series for fleet size
  - Can be enabled by include_plots and if matplotlib is available

8. CLI and usage

- Entry point: python -m yolo_router.cli --config configs/example_round_robin.json

  - \--arch overrides architecture in config
  - \--seed to override seed
  - \--output-dir to override reports location

- Exit code 0 with path to report files

- Logging: basic run summary to stdout

9. Testing strategy

- Deterministic seeds for reproducibility

- Unit tests:

  - RR selection order, rejection then retry behavior
  - LC selection chooses min outstanding under contention
  - Scale up invoked when no capacity; new target receives the stuck request
  - Scale down triggers under thresholds after 5 minutes simulated time
  - Metrics: retries histogram correctness, percentile calculations
  - Propagation delays in RR: non-initiating hosts see updates after sync_delay_ms

- Scenario tests:

  - Traffic spikes trigger scale up; cool-down triggers scale down
  - Max fleet cap respected

10. Milestones

- M1: Repo scaffolding, config loader, RNG/distributions helpers (1 day)
- M2: Core actors: Target, Topology, ScalingService, MetricsCollector, Sampler (2 days)
- M3: Round-robin LB with retry + scale behaviors; minimal report (2 days)
- M4: Least-conns LB with shared map, atomic updates; extend metrics (2 days)
- M5: Reporting: percentiles, histogram, writers; CLI wiring; example configs (1-2 days)
- M6: Tests, documentation, polish; optional plots (2 days)

11. Risks and mitigations

- Distribution fitting from percentiles can be tricky: start with lognormal fit; provide fallback to direct percentile table sampling via piecewise approximation.
- Time-weighted vs sample-based percentiles for concurrency/CPU: start with sample-based at fixed intervals; document the method.
- Propagation delays and race conditions: model via SimPy events and a simple message bus; ensure RR hosts gracefully handle removed targets.
- Performance on long runs: keep sampling interval configurable; avoid storing per-tick data where unnecessary.

12. Acceptance criteria

- Runs both architectures from a JSON config, with configurable LB hosts, targets, arrival/duration/cpu distributions, and scaling delays.

- RR: implements retry logic, sync delay on topology changes; scale up when retries exhausted; scale down idle targets.

- LC: implements atomic least-outstanding selection; scale up when no capacity; scale down idle targets.

- Generates final report with:

  - Histogram of retries per request
  - Percentiles p100, p99, p90, p80, p50 for target concurrent requests, request durations, request latencies, target CPU usage
  - Percentiles for concurrent number of target hosts in the fleet

- Deterministic behavior under fixed seed; example configs included.

Revised plan incorporating event-driven sampling and simulated time guarantees

A) Metrics collection: event-driven on every LB decision

- Replace periodic sampler with event-driven snapshots taken after every load balancing decision.

- Sampling trigger points (default):

  - After each assign(request) decision path:

    - Successful accept on a target
    - Rejection leading to retry
    - Exhausted retries → scale up invoked and, upon scaler completion, decision to send to new target

  - After scale up/down completion events that change fleet composition and are immediately followed by a routing decision

- Optional additional triggers (configurable, off by default to honor the request strictly):

  - On request service start/complete (to capture concurrency transitions without LB decisions)
  - On topology change (scale up/down completion even if no decision happens immediately)

- Snapshot contents recorded at env.now (simulated time):

  - Fleet: current active target count
  - Per-target: current_concurrency, instantaneous CPU utilization estimate (sum of cpu_demand of active requests / capacity), cumulative request count
  - Request-level (for the request being routed): retries_attempted, chosen target id (if accepted), accepted/rejected outcome

- Implementation details:

  - LBHostBase.expose a hook record_decision_snapshot(context) called at all decision outcomes in RoundRobinLBHost and LeastConnsLBHost.
  - MetricsCollector maintains event-series arrays keyed by metric name with entries {t=env.now, value, tags}.
  - To avoid duplicate entries at the same timestamp from rapid successive decisions, coalesce writes by (t, host_id) if needed.

B) Simulated time only (no wall time)

- All time is SimPy simulated time:

  - Use env.timeout(ms) for every delay: inter-arrival times, request durations, scaling delays, propagation/sync delays, and the “5 minutes” windows.
  - Use env.now (ms) as the single source of time for timestamps and durations.
  - Absolutely no time.sleep(), datetime.now(), or time.time() in the simulation code.

- Units and conversions:

  - Adopt milliseconds as the base unit across the codebase and config (duration_ms, scale_up_delay_ms, sync_delay_ms, etc.).
  - “5 minutes” thresholds computed as 5*60*1000 ms relative to env.now deltas.

- Sliding-window logic:
  - For “fewer than threshold in last 5 minutes”, keep per-target deques of event timestamps (in simulated ms) and evict entries older than env.now - window_ms.

C) Config updates

- Remove:
  - sampling_interval_ms (no periodic sampling)
- Add:
  - sampling: { mode: "event", sample_on: ["lb_decision"] }
    - sample_on may optionally include ["request_complete", "topology_change"] if user wants broader coverage later.
- Minor:
  - Ensure all delays and durations are …_ms fields; validate integers in ms.

D) Affected components and flow changes

- LB hosts (RoundRobin, LeastConns):

  - Insert calls to metrics.record_decision_snapshot after:

    - Each accept
    - Each reject (before retry)
    - After scale-up completion when routing to the new target

  - For RoundRobin: also record a snapshot after propagating a topology update to a host if it directly leads to a routing decision.

- ScalingService and Topology:
  - No metrics writes here by default; they emit events that LB hosts react to. If optional sample_on includes "topology_change", call metrics.snapshot there too.

- Targets:
  - Continue to update request-level metrics (latency, service duration, counts). If optional sample_on includes "request_complete", call metrics.snapshot on completion.

- Reporting:

  - Percentiles computed over event-driven samples rather than periodic samples.
  - Documentation note: event-based sampling focuses on system state at decision boundaries; enabling additional triggers increases fidelity for concurrency/CPU views between decisions.

E) Risks and mitigations

- Event burst creating many samples:

  - Coalesce multiple snapshots at identical env.now by host_id to a single merged snapshot.
  - Option to downsample during aggregation (e.g., keep every Nth event) if arrays grow large.

- Missing mid-flight state changes if only sampling at LB decisions:
  - Provide optional triggers (off by default) to capture starts/completions and topology changes; can be toggled without code changes.

- Consistency across hosts with propagation delays:
  - Snapshots reflect each host’s perceived state at env.now based on architecture rules (e.g., RR sync_delay_ms). Reports should note that decision-time state is host-local for RR and shared-map for LC.

F) Acceptance criteria deltas

- Sampler is event-driven: records a snapshot after every load balancing decision using env.now.
- No wall-clock usage in simulation. All delays, timers, and timestamps use env.now and env.timeout exclusively.
- Reports computed from event-based series meet the same required percentiles and histograms.

This revision keeps the plan intact while switching metrics capture to decision-time events and guarantees exclusive use of simulated time via SimPy.

```