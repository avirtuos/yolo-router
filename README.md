# yolo-router

A discrete-event simulation (SimPy) that was written entirely using spec driven development with OpenAI GPT5 + Cline in VSCode. For details on the design spec and prompts used
see design.md. The total cost for generating this simulation was $6.52 using a combination of combination of GPT-5 for planning and GPT-5-mini for acting.

The simulation presents supports supports two load-balancing architectures and their scaling behaviors under configurable traffic:

- Round Robin (RR)
- Least Connections (LC)

The simulation uses simulated time only (SimPy `env.now`/`env.timeout`), never wall-clock time. Metrics are captured in an event-driven way at every load balancing decision.

## Quickstart

1) Create a virtual environment and install dependencies:
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Run one of the example scenarios:
```
# Round Robin
python -m yolo_router --config configs/example_round_robin.json

# Least Connections
python -m yolo_router --config configs/example_least_conns.json
```

3) Find the reports:
- JSON: `reports/<scenario>/report.json`
- Markdown: `reports/<scenario>/report.md`
- HTML: `reports/<scenario>/report.html` (if `html` writer enabled)

4) CLI overrides (optional):
- `--arch round_robin|least_conns` override architecture
- `--seed <int>` set RNG seed for reproducibility
- `--output-dir <path>` change output directory

Example:
```
python -m yolo_router --config configs/example_round_robin.json --seed 999 --output-dir out/rr
```

## Configuration

Provide a JSON config file. The examples in `configs/` are good starting points.

Top-level sections:

- `simulation`
  - `seed` (int): RNG seed
  - `duration_ms` (int): simulated time to generate arrivals
  - `drain_ms` (int): time to allow in-flight requests to complete after generation stops
  - `total_time_ms` (int, optional): hard stop time for the entire test run (sim-time). If set, the simulation halts at this time regardless of drain settings.
- `traffic`
  - `arrival`: controls inter-arrival sampling
    - `distribution`: "poisson" | "exponential" | "normal" | "deterministic"
      - poisson: `rate_per_sec` (float)
      - exponential: `lambda` (events per ms, float)
      - normal: `mean_ms`, `stddev_ms` (float)
      - deterministic: `interval_ms` (float)
    - `ramp` (optional): configures request rate ramping over sim time (applies when `distribution` = "poisson")
      - `mode`: "none" | "linear" | "steps"
      - If `linear`:
        - `start_rate_per_sec` (float)
        - `end_rate_per_sec` (float)
        - `ramp_duration_ms` (int) — time to ramp from start to end
        - `hold_duration_ms` (int) — time to hold at end rate
      - If `steps`:
        - `steps`: array of objects { `t_ms`: int, `rate_per_sec`: float } applied piecewise by time
  - `request_duration_ms`: service time per request on a target
    - `mode`: "distribution" | "percentiles"
    - if `distribution`:
      - "lognormal": `mean_ms`, `sigma`
      - "normal": `mean_ms`, `stddev_ms`
      - "exponential": `lambda_ms` (1/mean_ms)
      - "deterministic": `value_ms`
    - if `percentiles`: `percentiles` object with `p50`, `p80`, `p90`, `p99`, `p100`
  - `cpu_demand`: per-request CPU demand (arbitrary units)
    - `mode`: "distribution" | "percentiles"
    - if `distribution`:
      - "normal": `mean`, `stddev`
      - "lognormal": `mean`, `sigma`
      - "exponential": `lambda` (1/mean)
      - "deterministic": `value`
    - if `percentiles`: `percentiles` object with `p50`, `p80`, `p90`, `p99`, `p100`
- `load_balancer`
  - `architecture`: "round_robin" | "least_conns"
  - `hosts` (int): number of LB hosts
  - `rr`:
    - `retry_limit` (int): LB retries on target rejection
    - `sync_delay_ms` (int): propagation delay for topology updates to other hosts
    - `idle_scale_down_threshold_requests_5m` (int): legacy threshold (see notes below)
  - `lc`:
    - `shared_map_lock_latency_ms` (int): optional lock latency overhead
    - `idle_scale_down_threshold_outstanding_5m` (int): legacy threshold (see notes below)
- `targets`
  - `initial_count` (int)
  - `max_concurrency_per_target` (int): hard concurrent request cap per target
  - `cpu_capacity_per_target` (float): CPU capacity unit per target
- `scaling`
  - `min_targets` (int): lower bound on fleet size
  - `max_targets` (int): upper bound on fleet size
  - `scale_up_delay_ms` (int)
  - `scale_down_delay_ms` (int)
  - `idle_scale_down_avg_concurrency_1m` (float, optional): new config to control scale-down selection — targets with trailing-1m average concurrency below this value are eligible for scale-down (default 1.0 if omitted).
- `sampling`
  - `mode`: "event" (event-driven sampling at LB decision boundaries)
  - `sample_on`: array; currently includes "lb_decision"
- `reporting`
  - `output_dir` (string)
  - `writers`: ["json", "markdown", "html"] (set of writers)

### Example: Round Robin config (excerpt)

```
{
  "simulation": { "seed": 123, "duration_ms": 60000, "drain_ms": 5000 },
  "traffic": {
    "arrival": { "distribution": "poisson", "rate_per_sec": 50.0 },
    "request_duration_ms": { "mode": "distribution", "distribution": "lognormal", "mean_ms": 100.0, "sigma": 0.5 },
    "cpu_demand": { "mode": "distribution", "distribution": "normal", "mean": 10.0, "stddev": 2.0 }
  },
  "load_balancer": {
    "architecture": "round_robin",
    "hosts": 3,
    "rr": { "retry_limit": 2, "sync_delay_ms": 100, "idle_scale_down_threshold_requests_5m": 2 }
  },
  "targets": {
    "initial_count": 4,
    "max_concurrency_per_target": 8,
    "cpu_capacity_per_target": 100.0
  },
  "scaling": {
    "min_targets": 1,
    "max_targets": 50,
    "scale_up_delay_ms": 200,
    "scale_down_delay_ms": 300,
    "idle_scale_down_avg_concurrency_1m": 1.0
  },
  "sampling": { "mode": "event", "sample_on": ["lb_decision"] },
  "reporting": { "output_dir": "reports/round_robin", "writers": ["json", "markdown", "html"] }
}
```

## What the reports show

Reports are generated at the end of the run:

- Retries histogram: counts how many requests required 0, 1, 2, ... retries.
- Percentiles (p100, p99, p90, p80, p50) for:
  - Per-target concurrent requests (from decision-time snapshots)
  - Per-target CPU utilization (from decision-time snapshots)
  - Request durations (service time on target)
  - End-to-end request latencies (LB arrival to completion)
- Fleet size percentiles (from decision-time snapshots)
- Per-target metrics (JSON and Markdown):
  - total_requests handled by each target
  - Percentiles for concurrency, CPU utilization, request duration, and latency

Visualizations in the HTML report:
- Latency Over Time:
  - Per-request latency scatter markers (every completed request)
  - Trailing 1-minute percentile lines: p99.99, p99.9, p99, p90, p80 (computed on the full completion stream)
- Latency Distribution (histogram) and Service Duration Distribution (histogram)
  - These histograms now use the full set of collected samples (no downsample), so they reflect all completed requests for the run.

Notes:
- Sampling is event-driven at every load-balancing decision, reflecting state at decision boundaries.
- The scale-down selection logic was updated to use a trailing 1-minute average concurrency per target (configurable via `scaling.idle_scale_down_avg_concurrency_1m`) rather than the previous "handled requests in trailing 5 minutes" counting approach. Both RR and LC scale-down scanners use the trailing-1m average concurrency criterion.
- If runs produce very large sample counts, the HTML histogram render may be heavier in the browser. If that becomes an issue we can:
  - Keep percentiles computed from the full sample set but downsample only for the histogram rendering (configurable cap).
  - Add a config flag to control histogram sample capping.
- All times are simulated time (ms). No wall-clock (e.g. `time.time()`/`sleep`) is used.

## Project layout

- `yolo_router/`
  - `__init__.py`: package metadata
  - `__main__.py`: module entrypoint (`python -m yolo_router`)
  - `cli.py`: main implementation and CLI
    - Configuration loader (with defaults)
    - Core simulation classes (see below)
    - Reporting and writers
- `configs/`
  - `example_round_robin.json`, `example_least_conns.json`: ready-to-run examples
- `requirements.txt`: Python dependencies
- `design.md`: original design brief
- `reports/`: output folder for generated reports (created at runtime)

## Key classes and functions (yolo_router/cli.py)

- Simulation driver
  - `class Simulation`: orchestrates the entire run
    - Initializes SimPy `env`, RNG, Topology, ScalingService, LB hosts, and targets
    - Request generator process samples inter-arrival times and submits requests to randomly chosen LB hosts
    - `run()`: advances simulated time, drains, and returns the report dict
- Traffic and request
  - `class Request`: request attributes (arrival time, duration, cpu_demand, retries, start/end times, chosen target)
  - Sampling helpers (functions) for arrival, duration, and cpu_demand distributions
- Targets and topology
  - `class Target`: accepts/denies requests based on `max_concurrency`; starts a service process; tracks current concurrency and CPU utilization; updates metrics on completion
  - `class Topology`: holds `Target` instances, manages IDs, broadcasts topology changes to LB hosts
- Scaling
  - `class ScalingService`: SimPy processes for `scale_up` and `scale_down` with configured delays; enforces `min_targets`/`max_targets`
- Load balancers
  - `class LBHostBase`: base class for LB hosts; defines the snapshot mechanism invoked after decisions
  - `class RoundRobinLBHost`:
    - Maintains per-host local list of targets and round-robin index
    - On rejection, retries up to `retry_limit`; then scales up if needed
    - Propagates topology changes to other hosts after `sync_delay_ms` (initiator learns immediately)
    - Periodic (sim-time) scan for idle targets to scale down (uses trailing-1m avg concurrency by default if configured)
  - `class SharedLeastConnsState`: shared outstanding count map protected by a `simpy.Resource` mutex; methods update counts with optional lock latency
  - `class LeastConnsLBHost`:
    - Atomically selects target with the least outstanding requests under `max_concurrency`
    - If none available, scales up and routes to new target (updating shared state)
    - Tracks completion via shared state; also performs idle scale-down scans (uses trailing-1m avg concurrency by default if configured)
- Metrics and reporting
  - `class MetricsCollector`:
    - Request-level metrics: retries, latencies, service durations
    - Event-driven snapshots after each LB decision: fleet size, per-target concurrency, per-target CPU utilization
    - Per-target aggregates: request counts and percentiles for concurrency, CPU utilization, durations, and latencies
    - `build_report()`: returns a dict consumed by writers
    - `report_markdown(report)`: returns a Markdown string
- CLI
  - `main(argv=None)`: parses arguments, loads/merges config, runs `Simulation`, writes JSON/Markdown/HTML reports

## Modeling notes

- Simulated time:
  - All timers/delays use `env.timeout(ms)` and `env.now` (ms). The 5-minute idle windows are based on simulated ms.
- Event-driven sampling:
  - Snapshots happen after every LB decision: accept, reject (before retry), and scale-then-route.
- RR propagation:
  - When scaling changes occur, the initiating host updates its local view immediately; others receive updates after `sync_delay_ms`.
- LC atomicity:
  - Target selection and outstanding count updates are protected by a single-capacity `simpy.Resource` to model a shared, consistent view.

## Reproducibility

- Set `simulation.seed` in config or `--seed` via CLI to get repeatable runs.

## Troubleshooting

- Missing dependencies:
  - Ensure you are in the virtual environment and ran `pip install -r requirements.txt`
- No reports:
  - Check that the configured `reporting.output_dir` is writable
  - Ensure the simulation `duration_ms` is non-zero and traffic arrival rate is reasonable

## License

This repository is provided for simulation and experimentation per the included design brief. Ensure you comply with any applicable licenses for dependencies listed in `requirements.txt`.
