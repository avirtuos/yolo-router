#!/usr/bin/env python3
"""
yolo-router simulation

Implements two load balancing strategies using SimPy simulated time:
- Round Robin (RR)
- Least Connections (LC)

Key features:
- Configurable traffic, durations, CPU demand via JSON config
- Simulated scaling service with scale up/down delays
- Event-driven sampling: snapshot after every LB decision (accept/reject/retry/scale-then-route)
- End-of-run report with:
  - Histogram of retries per request
  - Percentiles for per-target concurrent requests
  - Percentiles for request durations and end-to-end latencies
  - Percentiles for per-target CPU utilization
  - Percentiles for fleet size over time

Note:
- Simulated time units are milliseconds throughout (env.now/env.timeout(ms))
- No wall-clock time is used
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Iterable
from collections import deque, defaultdict, Counter

import numpy as np
import simpy


# -----------------------------
# Utilities and distributions
# -----------------------------
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def percentile(values: List[float], p: float) -> float:
    if not values:
        return float("nan")
    return float(np.percentile(values, p))


def safe_normal(rng: np.random.Generator, mean: float, stddev: float) -> float:
    # Truncated at 0
    v = rng.normal(mean, stddev)
    return max(v, 0.0)


# -----------------------------
# Request model
# -----------------------------
@dataclass
class Request:
    id: int
    arrival_time_ms: int
    duration_ms: int
    cpu_demand: float
    retries_attempted: int = 0
    start_service_time_ms: Optional[int] = None
    end_time_ms: Optional[int] = None
    chosen_target_id: Optional[int] = None

    @property
    def latency_ms(self) -> Optional[int]:
        if self.end_time_ms is None:
            return None
        return self.end_time_ms - self.arrival_time_ms

    @property
    def service_duration_ms(self) -> Optional[int]:
        if self.end_time_ms is None or self.start_service_time_ms is None:
            return None
        return self.end_time_ms - self.start_service_time_ms


# -----------------------------
# Metrics collection (event-driven)
# -----------------------------
class MetricsCollector:
    def __init__(self) -> None:
        # Request-level
        self.retries_hist: Counter = Counter()
        self.latencies: List[float] = []
        self.service_durations: List[float] = []

        # Event-driven snapshots
        self.fleet_size_samples: List[int] = []
        self.per_target_concurrency_samples: List[int] = []
        self.per_target_cpu_util_samples: List[float] = []

        # Optional: store decision events (can be large)
        self.decision_events: List[Dict[str, Any]] = []

        # Coalesce control
        self._last_event_by_host_time: Dict[Tuple[int, int], Dict[str, Any]] = {}

        # Per-target series and counts
        # Each entry: {"concurrency": List[int], "cpu": List[float], "durations": List[float], "latencies": List[float], "total": int}
        self.per_target: Dict[int, Dict[str, Any]] = defaultdict(lambda: {"concurrency": [], "cpu": [], "durations": [], "latencies": [], "total": 0})

        # Summary counters
        self.total_requests: int = 0
        self.scale_up_events: int = 0
        self.scale_down_events: int = 0
        self.scale_up_targets_added: int = 0
        self.scale_down_targets_removed: int = 0
        self.sim_time_ms: int = 0
        self.wall_runtime_seconds: Optional[float] = None

        # Time-series for request rate and latency over time
        self.arrival_times_ms: List[int] = []
        self.latency_times_ms: List[int] = []
        self.latency_values_ms: List[float] = []

    def record_request_arrival(self, t_ms: int) -> None:
        self.arrival_times_ms.append(int(t_ms))

    def record_request_completion(self, req: Request) -> None:
        self.retries_hist[req.retries_attempted] += 1
        self.total_requests += 1
        if req.latency_ms is not None:
            self.latencies.append(float(req.latency_ms))
        if req.service_duration_ms is not None:
            self.service_durations.append(float(req.service_duration_ms))
        # Latency time series
        if req.end_time_ms is not None and req.latency_ms is not None:
            self.latency_times_ms.append(int(req.end_time_ms))
            self.latency_values_ms.append(float(req.latency_ms))
        # Per-target request metrics
        if req.chosen_target_id is not None:
            series = self.per_target[req.chosen_target_id]
            if req.service_duration_ms is not None:
                series["durations"].append(float(req.service_duration_ms))
            if req.latency_ms is not None:
                series["latencies"].append(float(req.latency_ms))
            series["total"] = int(series.get("total", 0)) + 1

    def record_decision_snapshot(
        self,
        env: simpy.Environment,
        host_id: int,
        outcome: str,
        req: Request,
        chosen_target_id: Optional[int],
        targets: Iterable["Target"],
        fleet_size: int,
    ) -> None:
        # Coalesce by (t, host_id) if needed
        t = int(env.now)
        key = (t, host_id)

        # Snapshot fleet and per-target state
        self.fleet_size_samples.append(fleet_size)
        conc_vals = []
        cpu_vals = []
        for tgt in targets:
            conc = tgt.current_concurrency
            cpuu = tgt.cpu_utilization()
            conc_vals.append(conc)
            cpu_vals.append(cpuu)
            series = self.per_target[tgt.id]
            series["concurrency"].append(conc)
            series["cpu"].append(cpuu)

        self.per_target_concurrency_samples.extend(conc_vals)
        self.per_target_cpu_util_samples.extend(cpu_vals)

        event = {
            "t_ms": t,
            "host_id": host_id,
            "outcome": outcome,  # "accept" | "reject" | "scale_route"
            "request_id": req.id,
            "retries_attempted": req.retries_attempted,
            "chosen_target_id": chosen_target_id,
            "fleet_size": fleet_size,
            "avg_target_concurrency": float(np.mean(conc_vals)) if conc_vals else 0.0,
            "avg_target_cpu_util": float(np.mean(cpu_vals)) if cpu_vals else 0.0,
        }
        # Store only last event at this host/time
        self._last_event_by_host_time[key] = event

        # Also keep full list for traceability
        self.decision_events.append(event)

    def build_report(self) -> Dict[str, Any]:
        percentiles_list = [100, 99, 90, 80, 50]
        conc_percentiles = {f"p{int(p)}": percentile(self.per_target_concurrency_samples, p) for p in percentiles_list}
        cpu_percentiles = {f"p{int(p)}": percentile(self.per_target_cpu_util_samples, p) for p in percentiles_list}
        fleet_percentiles = {f"p{int(p)}": percentile(self.fleet_size_samples, p) for p in percentiles_list}
        duration_percentiles = {f"p{int(p)}": percentile(self.service_durations, p) for p in percentiles_list}
        latency_percentiles = {f"p{int(p)}": percentile(self.latencies, p) for p in percentiles_list}

        # retries histogram (include zero)
        retries_hist_dict = dict(sorted(self.retries_hist.items(), key=lambda kv: kv[0]))

        # Per-target aggregated metrics
        per_target_metrics: Dict[str, Any] = {}
        for tid, series in self.per_target.items():
            per_target_metrics[str(tid)] = {
                "total_requests": int(series.get("total", 0)),
                "concurrency_percentiles": {f"p{int(p)}": percentile(series["concurrency"], p) for p in percentiles_list},
                "cpu_util_percentiles": {f"p{int(p)}": percentile(series["cpu"], p) for p in percentiles_list},
                "request_duration_percentiles_ms": {f"p{int(p)}": percentile(series["durations"], p) for p in percentiles_list},
                "request_latency_percentiles_ms": {f"p{int(p)}": percentile(series["latencies"], p) for p in percentiles_list},
            }

        report = {
            "summary": {
                "sim_time_ms": int(self.sim_time_ms),
                "total_requests": int(self.total_requests),
                "scale_up_events": int(self.scale_up_events),
                "scale_down_events": int(self.scale_down_events),
                "scale_up_targets_added": int(self.scale_up_targets_added),
                "scale_down_targets_removed": int(self.scale_down_targets_removed),
                "wall_runtime_seconds": None if self.wall_runtime_seconds is None else float(self.wall_runtime_seconds),
            },
            "retries_histogram": retries_hist_dict,
            "per_target_concurrency_percentiles": conc_percentiles,
            "per_target_cpu_util_percentiles": cpu_percentiles,
            "fleet_size_percentiles": fleet_percentiles,
            "request_duration_percentiles_ms": duration_percentiles,
            "request_latency_percentiles_ms": latency_percentiles,
            "samples": {
                "num_decision_events": len(self.decision_events),
                "num_fleet_samples": len(self.fleet_size_samples),
                "num_target_concurrency_samples": len(self.per_target_concurrency_samples),
                "num_target_cpu_util_samples": len(self.per_target_cpu_util_samples),
            },
            "per_target_metrics": per_target_metrics,
        }
        return report

    def report_markdown(self, report: Dict[str, Any]) -> str:
        def dict_to_table(d: Dict[str, Any]) -> str:
            keys = list(d.keys())
            vals = [d[k] for k in keys]
            keys_str = [str(k) for k in keys]
            return (
                "| " + " | ".join(keys_str) + " |\n" +
                "| " + " | ".join(["---"] * len(keys_str)) + " |\n" +
                "| " + " | ".join(str(v) for v in vals) + " |\n"
            )

        md = []
        md.append("# yolo-router Simulation Report\n")
        # Run summary
        summary = report.get("summary", {})
        if summary:
            md.append("## Run Summary\n")
            md.append(dict_to_table({
                "sim_time_ms": summary.get("sim_time_ms", "n/a"),
                "total_requests": summary.get("total_requests", "n/a"),
                "scale_up_events": summary.get("scale_up_events", "n/a"),
                "scale_down_events": summary.get("scale_down_events", "n/a"),
                "scale_up_targets_added": summary.get("scale_up_targets_added", "n/a"),
                "scale_down_targets_removed": summary.get("scale_down_targets_removed", "n/a"),
                "wall_runtime_seconds": summary.get("wall_runtime_seconds", "n/a"),
            }))
        md.append("## Retries Histogram\n")
        if report["retries_histogram"]:
            md.append(dict_to_table(report["retries_histogram"]))
        else:
            md.append("_No retries recorded._\n")
        md.append("\n## Per-Target Concurrent Requests (percentiles)\n")
        md.append(dict_to_table(report["per_target_concurrency_percentiles"]))
        md.append("\n## Per-Target CPU Utilization (percentiles)\n")
        md.append(dict_to_table(report["per_target_cpu_util_percentiles"]))
        md.append("\n## Fleet Size (percentiles)\n")
        md.append(dict_to_table(report["fleet_size_percentiles"]))
        md.append("\n## Request Duration (ms) (percentiles)\n")
        md.append(dict_to_table(report["request_duration_percentiles_ms"]))
        md.append("\n## Request Latency (ms) (percentiles)\n")
        md.append(dict_to_table(report["request_latency_percentiles_ms"]))
        md.append("\n")
        md.append("## Sample Counts\n")
        md.append(dict_to_table(report["samples"]))

        # Per-target metrics
        pt = report.get("per_target_metrics", {})
        if pt:
            md.append("\n## Per-Target Metrics\n")
            # Sort by numeric target id if possible
            def _key_fn(x):
                try:
                    return int(x)
                except Exception:
                    return str(x)
            for tid in sorted(pt.keys(), key=_key_fn):
                pm = pt[tid]
                md.append(f"\n### Target {tid}\n")
                md.append(dict_to_table({"total_requests": pm.get("total_requests", 0)}))
                md.append("\nConcurrency percentiles\n")
                md.append(dict_to_table(pm.get("concurrency_percentiles", {})))
                md.append("\nCPU utilization percentiles\n")
                md.append(dict_to_table(pm.get("cpu_util_percentiles", {})))
                md.append("\nRequest duration (ms) percentiles\n")
                md.append(dict_to_table(pm.get("request_duration_percentiles_ms", {})))
                md.append("\nRequest latency (ms) percentiles\n")
                md.append(dict_to_table(pm.get("request_latency_percentiles_ms", {})))

        return "".join(md)


# -----------------------------
# Target and topology
# -----------------------------
class Target:
    def __init__(self, env: simpy.Environment, id_: int, max_concurrency: int, cpu_capacity: float) -> None:
        self.env = env
        self.id = id_
        self.max_concurrency = max_concurrency
        self.cpu_capacity = cpu_capacity

        self.current_concurrency: int = 0
        self._active_cpu_demand_sum: float = 0.0
        # request completions for RR scale-down policy (timestamps in ms)
        self._handled_timestamps_ms: deque[int] = deque()

    def can_accept(self) -> bool:
        return self.current_concurrency < self.max_concurrency

    def cpu_utilization(self) -> float:
        if self.cpu_capacity <= 0:
            return 0.0
        return self._active_cpu_demand_sum / self.cpu_capacity

    def _record_handled(self, t_ms: int) -> None:
        self._handled_timestamps_ms.append(t_ms)

    def _evict_old_handled(self, now_ms: int, window_ms: int) -> None:
        while self._handled_timestamps_ms and self._handled_timestamps_ms[0] < now_ms - window_ms:
            self._handled_timestamps_ms.popleft()

    def handled_in_window(self, now_ms: int, window_ms: int) -> int:
        self._evict_old_handled(now_ms, window_ms)
        return len(self._handled_timestamps_ms)

    def accept(self, req: Request, lb_host: "LBHostBase") -> bool:
        if not self.can_accept():
            return False
        # accept and start service
        self.current_concurrency += 1
        self._active_cpu_demand_sum += req.cpu_demand
        req.start_service_time_ms = int(self.env.now)
        # service process
        self.env.process(self._service(req, lb_host))
        return True

    def _service(self, req: Request, lb_host: "LBHostBase"):
        # Phase 1: no slowdown; effective duration equals request duration
        yield self.env.timeout(req.duration_ms)
        req.end_time_ms = int(self.env.now)
        # Update counters
        self.current_concurrency -= 1
        self._active_cpu_demand_sum -= req.cpu_demand
        # Track request handled for RR scale-down policy
        self._record_handled(int(self.env.now))
        # Notify LB of completion (LC needs to decrement outstanding)
        lb_host.on_request_complete(self.id)
        # Record metrics
        lb_host.metrics.record_request_completion(req)


class Topology:
    def __init__(self, env: simpy.Environment) -> None:
        self.env = env
        self._targets: Dict[int, Target] = {}
        self._subscribers: List["LBHostBase"] = []
        self._next_target_id: int = 1

    def subscribe(self, host: "LBHostBase") -> None:
        self._subscribers.append(host)

    def new_target_id(self) -> int:
        tid = self._next_target_id
        self._next_target_id += 1
        return tid

    def add_target(self, target: Target, initiator_host_id: Optional[int] = None) -> None:
        self._targets[target.id] = target
        # Broadcast change
        for host in self._subscribers:
            host.notify_topology_change(initiator_host_id)

    def remove_target(self, target_id: int, initiator_host_id: Optional[int] = None) -> None:
        if target_id in self._targets:
            del self._targets[target_id]
            for host in self._subscribers:
                host.notify_topology_change(initiator_host_id)

    def get_target(self, target_id: int) -> Optional[Target]:
        return self._targets.get(target_id)

    def list_targets(self) -> List[Target]:
        return list(self._targets.values())

    def list_target_ids(self) -> List[int]:
        return list(self._targets.keys())

    def fleet_size(self) -> int:
        return len(self._targets)


# -----------------------------
# Scaling service
# -----------------------------
class ScalingService:
    def __init__(
        self,
        env: simpy.Environment,
        topology: Topology,
        max_concurrency_per_target: int,
        cpu_capacity_per_target: float,
        delays: Dict[str, int],  # {scale_up_delay_ms, scale_down_delay_ms}
        metrics: "MetricsCollector",
    ) -> None:
        self.env = env
        self.topology = topology
        self.max_concurrency_per_target = max_concurrency_per_target
        self.cpu_capacity_per_target = cpu_capacity_per_target
        self.scale_up_delay_ms = int(delays.get("scale_up_delay_ms", 0))
        self.scale_down_delay_ms = int(delays.get("scale_down_delay_ms", 0))
        self.min_targets = int(delays.get("min_targets", 1))
        self.max_targets = int(delays.get("max_targets", 1000000))
        self.metrics = metrics

    def scale_up(self, count: int, initiator_host_id: Optional[int]) -> simpy.events.Event:
        # Returns an event that completes after the targets are added
        return self.env.process(self._scale_up_proc(count, initiator_host_id))

    def scale_down(self, target_id: int, initiator_host_id: Optional[int]) -> simpy.events.Event:
        return self.env.process(self._scale_down_proc(target_id, initiator_host_id))

    def _scale_up_proc(self, count: int, initiator_host_id: Optional[int]):
        yield self.env.timeout(self.scale_up_delay_ms)
        created_ids: List[int] = []
        available = max(0, self.max_targets - self.topology.fleet_size())
        to_create = min(count, available)
        if to_create <= 0:
            return created_ids
        for _ in range(to_create):
            tid = self.topology.new_target_id()
            tgt = Target(
                env=self.env,
                id_=tid,
                max_concurrency=self.max_concurrency_per_target,
                cpu_capacity=self.cpu_capacity_per_target,
            )
            self.topology.add_target(tgt, initiator_host_id=initiator_host_id)
            created_ids.append(tid)
        # record event after successful additions
        self.metrics.scale_up_events += 1
        self.metrics.scale_up_targets_added += to_create
        return created_ids

    def _scale_down_proc(self, target_id: int, initiator_host_id: Optional[int]):
        yield self.env.timeout(self.scale_down_delay_ms)
        if self.topology.fleet_size() <= self.min_targets:
            return False
        self.topology.remove_target(target_id, initiator_host_id=initiator_host_id)
        # record event after successful removal
        self.metrics.scale_down_events += 1
        self.metrics.scale_down_targets_removed += 1
        return True


# -----------------------------
# Load balancers
# -----------------------------
class LBHostBase:
    def __init__(
        self,
        env: simpy.Environment,
        host_id: int,
        topology: Topology,
        scaler: ScalingService,
        metrics: MetricsCollector,
        config: Dict[str, Any],
    ) -> None:
        self.env = env
        self.host_id = host_id
        self.topology = topology
        self.scaler = scaler
        self.metrics = metrics
        self.config = config

        self.topology.subscribe(self)

    # Public interface
    def handle_request(self, req: Request):
        raise NotImplementedError

    def on_request_complete(self, target_id: int) -> None:
        # Default: no-op; LC overrides to decrement outstanding
        pass

    # Notifications
    def notify_topology_change(self, initiator_host_id: Optional[int]) -> None:
        # Default: no-op; RR overrides to synchronize with delay
        pass

    # Helper to snapshot after decisions
    def snapshot(self, outcome: str, req: Request, chosen_target_id: Optional[int]) -> None:
        self.metrics.record_decision_snapshot(
            env=self.env,
            host_id=self.host_id,
            outcome=outcome,
            req=req,
            chosen_target_id=chosen_target_id,
            targets=self.topology.list_targets(),
            fleet_size=self.topology.fleet_size(),
        )


class RoundRobinLBHost(LBHostBase):
    def __init__(
        self,
        env: simpy.Environment,
        host_id: int,
        topology: Topology,
        scaler: ScalingService,
        metrics: MetricsCollector,
        config: Dict[str, Any],
    ) -> None:
        super().__init__(env, host_id, topology, scaler, metrics, config)
        rr_cfg = config.get("load_balancer", {}).get("rr", {})
        self.retry_limit: int = int(rr_cfg.get("retry_limit", 2))
        self.sync_delay_ms: int = int(rr_cfg.get("sync_delay_ms", 100))
        self.idle_threshold_requests_5m: int = int(rr_cfg.get("idle_scale_down_threshold_requests_5m", 1))
        # Local copy of target IDs and round-robin index
        self._local_target_ids: List[int] = self.topology.list_target_ids()
        self._rr_index: int = 0
        # Start scale-down scanning task
        self.env.process(self._scale_down_scanner())

    def notify_topology_change(self, initiator_host_id: Optional[int]) -> None:
        # Propagate changes to non-initiators after sync_delay_ms
        delay = 0 if initiator_host_id == self.host_id else self.sync_delay_ms
        self.env.process(self._sync_local_after_delay(delay))

    def _sync_local_after_delay(self, delay_ms: int):
        yield self.env.timeout(delay_ms)
        self._local_target_ids = self.topology.list_target_ids()
        # Keep rr index in range
        if self._local_target_ids:
            self._rr_index = self._rr_index % len(self._local_target_ids)
        else:
            self._rr_index = 0

    def _next_target_id(self) -> Optional[int]:
        if not self._local_target_ids:
            return None
        tid = self._local_target_ids[self._rr_index]
        self._rr_index = (self._rr_index + 1) % len(self._local_target_ids)
        return tid

    def handle_request(self, req: Request):
        # Attempt assign with retries
        attempts = 0
        while attempts <= self.retry_limit:
            tid = self._next_target_id()
            if tid is None:
                # No targets; must scale up
                created_ids = yield self.scaler.scale_up(count=1, initiator_host_id=self.host_id)
                # Immediate local knowledge (initiator sees it instantly)
                yield self.env.timeout(0)
                # Update local view immediately
                self._local_target_ids = self.topology.list_target_ids()
                if created_ids:
                    tid_new = created_ids[0]
                    target = self.topology.get_target(tid_new)
                    if target and target.accept(req, self):
                        req.chosen_target_id = tid_new
                        self.snapshot("scale_route", req, chosen_target_id=tid_new)
                        return
                # If somehow still no target, loop again
                self.snapshot("reject", req, chosen_target_id=None)
                attempts += 1
                req.retries_attempted = attempts
                continue

            target = self.topology.get_target(tid)
            if target and target.accept(req, self):
                req.chosen_target_id = tid
                self.snapshot("accept", req, chosen_target_id=tid)
                return
            else:
                # Rejected due to concurrency
                self.snapshot("reject", req, chosen_target_id=None)
                attempts += 1
                req.retries_attempted = attempts

        # Exhausted retries: scale up and route to new target
        created_ids = yield self.scaler.scale_up(count=1, initiator_host_id=self.host_id)
        yield self.env.timeout(0)
        self._local_target_ids = self.topology.list_target_ids()
        if created_ids:
            tid_new = created_ids[0]
            target = self.topology.get_target(tid_new)
            if target and target.accept(req, self):
                req.chosen_target_id = tid_new
                self.snapshot("scale_route", req, chosen_target_id=tid_new)
                return
        # If no success, drop (should not happen if scale up worked); still record
        self.snapshot("reject", req, chosen_target_id=None)

    def _scale_down_scanner(self):
        # Periodically (sim-time) check for idle targets and scale them down
        window_ms = 5 * 60 * 1000
        scan_interval_ms = 60 * 1000  # once per minute
        scaling_cfg = self.config.get("scaling", {})
        min_targets = int(scaling_cfg.get("min_targets", 1))
        while True:
            yield self.env.timeout(scan_interval_ms)
            # For RR, any host may initiate scale down; we don't coordinate here for simplicity
            # Only scale down targets with zero concurrency and few handled in window
            target_ids = self.topology.list_target_ids()
            if len(target_ids) <= min_targets:
                continue
            for tid in list(target_ids):
                target = self.topology.get_target(tid)
                if target is None:
                    continue
                if target.current_concurrency > 0:
                    continue
                count = target.handled_in_window(now_ms=int(self.env.now), window_ms=window_ms)
                if count < self.idle_threshold_requests_5m and len(self.topology.list_target_ids()) > min_targets:
                    # Initiate scale down
                    yield self.scaler.scale_down(target_id=tid, initiator_host_id=self.host_id)
                    # Local view updates will propagate via topology notifications


class SharedLeastConnsState:
    def __init__(self, env: simpy.Environment, topology: Topology, lock_latency_ms: int) -> None:
        self.env = env
        self.topology = topology
        self.lock = simpy.Resource(env, capacity=1)
        self.lock_latency_ms = lock_latency_ms
        # outstanding count per target
        self.outstanding: Dict[int, int] = defaultdict(int)
        # For scale-down policy: track when outstanding dropped below threshold
        self.below_since_ms: Dict[int, Optional[int]] = defaultdict(lambda: None)

    def on_assign(self, target_id: int) -> simpy.events.Event:
        return self.env.process(self._on_assign_proc(target_id))

    def _on_assign_proc(self, target_id: int):
        with self.lock.request() as req:
            yield req
            if self.lock_latency_ms:
                yield self.env.timeout(self.lock_latency_ms)
            self.outstanding[target_id] += 1
            # If outstanding rises, reset below_since
            self.below_since_ms[target_id] = None

    def on_complete(self, target_id: int) -> simpy.events.Event:
        return self.env.process(self._on_complete_proc(target_id))

    def _on_complete_proc(self, target_id: int):
        with self.lock.request() as req:
            yield req
            if self.lock_latency_ms:
                yield self.env.timeout(self.lock_latency_ms)
            self.outstanding[target_id] = max(0, self.outstanding[target_id] - 1)
            # If now low, mark time
            if self.outstanding[target_id] == 0:
                if self.below_since_ms[target_id] is None:
                    self.below_since_ms[target_id] = int(self.env.now)


class LeastConnsLBHost(LBHostBase):
    def __init__(
        self,
        env: simpy.Environment,
        host_id: int,
        topology: Topology,
        scaler: ScalingService,
        metrics: MetricsCollector,
        config: Dict[str, Any],
        shared_state: SharedLeastConnsState,
    ) -> None:
        super().__init__(env, host_id, topology, scaler, metrics, config)
        lc_cfg = config.get("load_balancer", {}).get("lc", {})
        self.lock_latency_ms: int = int(lc_cfg.get("shared_map_lock_latency_ms", 0))
        self.idle_threshold_outstanding_5m: int = int(lc_cfg.get("idle_scale_down_threshold_outstanding_5m", 1))
        self.shared = shared_state
        # Start scale-down scanning
        self.env.process(self._scale_down_scanner())

    def handle_request(self, req: Request):
        # Try to find target with minimal outstanding < max concurrency
        # We need to read topology and outstanding atomically
        with self.shared.lock.request() as lk:
            yield lk
            if self.lock_latency_ms:
                yield self.env.timeout(self.lock_latency_ms)
            candidates: List[Tuple[int, int]] = []  # (outstanding, target_id)
            for tgt in self.topology.list_targets():
                out = self.shared.outstanding.get(tgt.id, 0)
                if out < tgt.max_concurrency:
                    candidates.append((out, tgt.id))
            if candidates:
                candidates.sort()
                _, tid = candidates[0]
                # Reserve slot: increment outstanding
                self.shared.outstanding[tid] = self.shared.outstanding.get(tid, 0) + 1
                chosen = self.topology.get_target(tid)
                # Release lock before accept to avoid holding during service start
            else:
                chosen = None

        if chosen:
            # Send to chosen
            if chosen.accept(req, self):
                req.chosen_target_id = chosen.id
                self.snapshot("accept", req, chosen_target_id=chosen.id)
                return
            else:
                # Should be rare: target rejected despite reservation (race). Treat as reject and retry via scale up.
                req.retries_attempted += 1
                self.snapshot("reject", req, chosen_target_id=None)
                # Undo the optimistic reservation since accept failed
                with self.shared.lock.request() as lk3:
                    yield lk3
                    if self.lock_latency_ms:
                        yield self.env.timeout(self.lock_latency_ms)
                    self.shared.outstanding[chosen.id] = max(0, self.shared.outstanding.get(chosen.id, 0) - 1)

        # No capacity across fleet: scale up and route
        created_ids = yield self.scaler.scale_up(count=1, initiator_host_id=self.host_id)
        if created_ids:
            tid_new = created_ids[0]
            # Add to shared map with outstanding=1 and route
            with self.shared.lock.request() as lk2:
                yield lk2
                if self.lock_latency_ms:
                    yield self.env.timeout(self.lock_latency_ms)
                self.shared.outstanding[tid_new] = 1
            new_target = self.topology.get_target(tid_new)
            if new_target and new_target.accept(req, self):
                req.chosen_target_id = tid_new
                self.snapshot("scale_route", req, chosen_target_id=tid_new)
                return
        # If still no success
        self.snapshot("reject", req, chosen_target_id=None)

    def on_request_complete(self, target_id: int) -> None:
        # Decrement outstanding (SharedLeastConnsState schedules its own process)
        self.shared.on_complete(target_id)

    def _scale_down_scanner(self):
        window_ms = 5 * 60 * 1000
        scan_interval_ms = 60 * 1000
        scaling_cfg = self.config.get("scaling", {})
        min_targets = int(scaling_cfg.get("min_targets", 1))
        while True:
            yield self.env.timeout(scan_interval_ms)
            # With lock, evaluate scale-down candidates
            with self.shared.lock.request() as lk:
                yield lk
                if self.lock_latency_ms:
                    yield self.env.timeout(self.lock_latency_ms)
                # Evaluate targets
                target_ids = self.topology.list_target_ids()
                if len(target_ids) <= min_targets:
                    continue
                now = int(self.env.now)
                for tid in list(target_ids):
                    tgt = self.topology.get_target(tid)
                    if tgt is None:
                        continue
                    out = self.shared.outstanding.get(tid, 0)
                    below_since = self.shared.below_since_ms.get(tid, None)
                    # If outstanding is below threshold for >= 5 minutes and no concurrency on target, scale down
                    cond_low = out < self.idle_threshold_outstanding_5m
                    cond_time = below_since is not None and (now - below_since) >= window_ms
                    if cond_low and cond_time and tgt.current_concurrency == 0 and len(self.topology.list_target_ids()) > min_targets:
                        # Initiate scale down
                        yield self.scaler.scale_down(target_id=tid, initiator_host_id=self.host_id)
                        # Clean up shared maps
                        self.shared.outstanding.pop(tid, None)
                        self.shared.below_since_ms.pop(tid, None)


# -----------------------------
# Simulation driver
# -----------------------------
class Simulation:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        sim_cfg = config.get("simulation", {})
        self.seed = int(sim_cfg.get("seed", 42))
        self.duration_ms = int(sim_cfg.get("duration_ms", 60_000))
        # Optional total test time; if provided, we stop the whole simulation at this time
        self.total_time_ms: Optional[int] = sim_cfg.get("total_time_ms")
        if self.total_time_ms is not None:
            self.total_time_ms = int(self.total_time_ms)
            # Generation stops no later than total_time_ms to allow in-flight within the fixed test time
            self.generation_stop_ms = min(self.duration_ms, self.total_time_ms)
        else:
            self.generation_stop_ms = self.duration_ms
        self.rng = np.random.default_rng(self.seed)

        self.env = simpy.Environment()
        self.topology = Topology(self.env)
        targets_cfg = config.get("targets", {})
        self.max_concurrency_per_target = int(targets_cfg.get("max_concurrency_per_target", 10))
        self.cpu_capacity_per_target = float(targets_cfg.get("cpu_capacity_per_target", 100.0))

        # Initialize metrics before services that will record into it
        self.metrics = MetricsCollector()

        self.scaler = ScalingService(
            env=self.env,
            topology=self.topology,
            max_concurrency_per_target=self.max_concurrency_per_target,
            cpu_capacity_per_target=self.cpu_capacity_per_target,
            delays=config.get("scaling", {}),
            metrics=self.metrics,
        )

        # LB hosts
        lb_cfg = config.get("load_balancer", {})
        arch = lb_cfg.get("architecture", "round_robin")
        self.num_hosts = int(lb_cfg.get("hosts", 1))
        self.lb_hosts: List[LBHostBase] = []
        if arch == "least_conns":
            lc_cfg = lb_cfg.get("lc", {})
            shared = SharedLeastConnsState(
                env=self.env,
                topology=self.topology,
                lock_latency_ms=int(lc_cfg.get("shared_map_lock_latency_ms", 0)),
            )
            for i in range(self.num_hosts):
                self.lb_hosts.append(
                    LeastConnsLBHost(
                        env=self.env,
                        host_id=i,
                        topology=self.topology,
                        scaler=self.scaler,
                        metrics=self.metrics,
                        config=self.config,
                        shared_state=shared,
                    )
                )
        else:
            for i in range(self.num_hosts):
                self.lb_hosts.append(
                    RoundRobinLBHost(
                        env=self.env,
                        host_id=i,
                        topology=self.topology,
                        scaler=self.scaler,
                        metrics=self.metrics,
                        config=self.config,
                    )
                )

        # Initialize targets
        initial_targets = int(targets_cfg.get("initial_count", 1))
        for _ in range(initial_targets):
            tid = self.topology.new_target_id()
            tgt = Target(
                env=self.env,
                id_=tid,
                max_concurrency=self.max_concurrency_per_target,
                cpu_capacity=self.cpu_capacity_per_target,
            )
            self.topology.add_target(tgt, initiator_host_id=None)

    def _progress_logger(self, total_sim_ms: int, interval_ms: int = 60_000):
        """Background process that logs progress every interval_ms of simulated time.

        Logs:
        - simulated minutes elapsed
        - simulated minutes remaining
        - wall time elapsed (seconds)
        - estimated wall time remaining (seconds) using linear scaling
        """
        while True:
            yield self.env.timeout(interval_ms)
            elapsed_sim = int(self.env.now)
            remaining_sim = max(0, total_sim_ms - elapsed_sim)
            elapsed_min = elapsed_sim / 60000.0
            remaining_min = remaining_sim / 60000.0
            wall_elapsed = time.time() - getattr(self, "_wall_start", time.time())
            est_wall_remaining = None
            if elapsed_sim > 0:
                est_wall_remaining = wall_elapsed * (remaining_sim / float(elapsed_sim))
            # Print a concise progress log line
            if est_wall_remaining is None:
                print(f"[sim-progress] sim_elapsed_min={elapsed_min:.2f} min, sim_remaining_min={remaining_min:.2f} min, wall_elapsed_s={wall_elapsed:.2f}")
            else:
                print(f"[sim-progress] sim_elapsed_min={elapsed_min:.2f} min, sim_remaining_min={remaining_min:.2f} min, wall_elapsed_s={wall_elapsed:.2f}, est_wall_remaining_s={est_wall_remaining:.2f}")

    # --------------- Traffic generation ---------------
    def _current_rate_per_sec(self, now_ms: int, cfg: Dict[str, Any]) -> float:
        """Compute the current arrival rate per second, applying optional ramp settings."""
        base_rate = float(cfg.get("rate_per_sec", 10.0))
        ramp = cfg.get("ramp", {"mode": "none"})
        mode = ramp.get("mode", "none")
        if mode == "linear":
            start = float(ramp.get("start_rate_per_sec", base_rate))
            end = float(ramp.get("end_rate_per_sec", base_rate))
            ramp_dur = int(ramp.get("ramp_duration_ms", 0))
            hold_dur = int(ramp.get("hold_duration_ms", 0))
            if ramp_dur <= 0:
                return end
            if now_ms <= ramp_dur:
                return start + (end - start) * (now_ms / float(ramp_dur))
            elif now_ms <= ramp_dur + hold_dur:
                return end
            else:
                return end
        elif mode == "steps":
            # steps: array of {t_ms, rate_per_sec}; choose last with t_ms <= now
            steps = ramp.get("steps", [])
            current = base_rate
            for step in steps:
                t_ms = int(step.get("t_ms", 0))
                r = float(step.get("rate_per_sec", current))
                if now_ms >= t_ms:
                    current = r
            return current
        else:
            return base_rate

    def _sample_interarrival_ms(self, cfg: Dict[str, Any], now_ms: int) -> int:
        dist = cfg.get("distribution", "poisson")
        if dist == "poisson":
            rate_per_sec = self._current_rate_per_sec(now_ms, cfg)
            mean_ms = 1000.0 / max(rate_per_sec, 1e-9)
            return int(max(0.0, self.rng.exponential(mean_ms)))
        elif dist == "exponential":
            lam = float(cfg.get("lambda", 1.0))  # events per ms
            if lam <= 0:
                return 0
            return int(max(0.0, self.rng.exponential(1.0 / lam)))
        elif dist == "normal":
            mean = float(cfg.get("mean_ms", 100.0))
            std = float(cfg.get("stddev_ms", 10.0))
            return int(safe_normal(self.rng, mean, std))
        elif dist == "deterministic":
            interval = float(cfg.get("interval_ms", 100.0))
            return int(interval)
        else:
            # default to poisson
            rate_per_sec = float(cfg.get("rate_per_sec", 10.0))
            mean_ms = 1000.0 / max(rate_per_sec, 1e-9)
            return int(max(0.0, self.rng.exponential(mean_ms)))

    def _sample_duration_ms(self, cfg: Dict[str, Any]) -> int:
        mode = cfg.get("mode", "distribution")
        if mode == "percentiles":
            # Simple piecewise approximation: use p50 with random tail selection
            p = cfg.get("percentiles", {})
            # fallbacks
            p50 = float(p.get("p50", 100.0))
            p80 = float(p.get("p80", p50))
            p90 = float(p.get("p90", p80))
            p99 = float(p.get("p99", p90))
            p100 = float(p.get("p100", p99))
            u = self.rng.random()
            if u < 0.5:
                val = p50 * (u / 0.5)  # 0..p50
            elif u < 0.8:
                val = p50 + (p80 - p50) * ((u - 0.5) / 0.3)
            elif u < 0.9:
                val = p80 + (p90 - p80) * ((u - 0.8) / 0.1)
            elif u < 0.99:
                val = p90 + (p99 - p90) * ((u - 0.9) / 0.09)
            else:
                val = p99 + (p100 - p99) * ((u - 0.99) / 0.01)
            return int(max(0.0, val))
        else:
            dist = cfg.get("distribution", "lognormal")
            if dist == "lognormal":
                mean = float(cfg.get("mean_ms", 100.0))
                sigma = float(cfg.get("sigma", 0.5))
                return int(max(0.0, self.rng.lognormal(mean=math.log(max(mean, 1e-9)), sigma=sigma)))
            elif dist == "normal":
                mean = float(cfg.get("mean_ms", 100.0))
                std = float(cfg.get("stddev_ms", 10.0))
                return int(safe_normal(self.rng, mean, std))
            elif dist == "exponential":
                lam = float(cfg.get("lambda_ms", 0.01))  # 1/mean_ms
                if lam <= 0:
                    return 0
                return int(max(0.0, self.rng.exponential(1.0 / lam)))
            elif dist == "deterministic":
                return int(cfg.get("value_ms", 100.0))
            else:
                mean = float(cfg.get("mean_ms", 100.0))
                std = float(cfg.get("stddev_ms", 10.0))
                return int(safe_normal(self.rng, mean, std))

    def _sample_cpu_demand(self, cfg: Dict[str, Any]) -> float:
        mode = cfg.get("mode", "distribution")
        if mode == "percentiles":
            p = cfg.get("percentiles", {})
            p50 = float(p.get("p50", 10.0))
            p80 = float(p.get("p80", p50))
            p90 = float(p.get("p90", p80))
            p99 = float(p.get("p99", p90))
            p100 = float(p.get("p100", p99))
            u = self.rng.random()
            if u < 0.5:
                val = p50 * (u / 0.5)
            elif u < 0.8:
                val = p50 + (p80 - p50) * ((u - 0.5) / 0.3)
            elif u < 0.9:
                val = p80 + (p90 - p80) * ((u - 0.8) / 0.1)
            elif u < 0.99:
                val = p90 + (p99 - p90) * ((u - 0.9) / 0.09)
            else:
                val = p99 + (p100 - p99) * ((u - 0.99) / 0.01)
            return max(0.0, val)
        else:
            dist = cfg.get("distribution", "normal")
            if dist == "normal":
                mean = float(cfg.get("mean", 10.0))
                std = float(cfg.get("stddev", 2.0))
                return float(safe_normal(self.rng, mean, std))
            elif dist == "lognormal":
                mean = float(cfg.get("mean", 10.0))
                sigma = float(cfg.get("sigma", 0.5))
                return float(self.rng.lognormal(mean=math.log(max(mean, 1e-9)), sigma=sigma))
            elif dist == "exponential":
                lam = float(cfg.get("lambda", 0.1))  # 1/mean
                if lam <= 0:
                    return 0.0
                return float(self.rng.exponential(1.0 / lam))
            elif dist == "deterministic":
                return float(cfg.get("value", 10.0))
            else:
                mean = float(cfg.get("mean", 10.0))
                std = float(cfg.get("stddev", 2.0))
                return float(safe_normal(self.rng, mean, std))

    def _request_generator(self):
        traffic_cfg = self.config.get("traffic", {})
        arrival_cfg = traffic_cfg.get("arrival", {"distribution": "poisson", "rate_per_sec": 10})
        dur_cfg = traffic_cfg.get("request_duration_ms", {"mode": "distribution", "distribution": "lognormal", "mean_ms": 100, "sigma": 0.5})
        cpu_cfg = traffic_cfg.get("cpu_demand", {"mode": "distribution", "distribution": "normal", "mean": 10, "stddev": 2})

        next_id = 1
        while True:
            now = int(self.env.now)
            if now >= self.generation_stop_ms:
                return
            # Generate request
            req = Request(
                id=next_id,
                arrival_time_ms=now,
                duration_ms=self._sample_duration_ms(dur_cfg),
                cpu_demand=self._sample_cpu_demand(cpu_cfg),
            )
            next_id += 1
            # Record arrival for rate chart
            self.metrics.record_request_arrival(now)
            # Choose a host uniformly
            host = self.lb_hosts[self.rng.integers(0, len(self.lb_hosts))]
            # Spawn handling process
            self.env.process(host.handle_request(req))
            # Wait for next arrival
            inter_ms = self._sample_interarrival_ms(arrival_cfg, now)
            yield self.env.timeout(inter_ms)

    def run(self) -> Dict[str, Any]:
        # Start wall timer used for progress estimation and logging
        self._wall_start = time.time()
        # Start request generator
        self.env.process(self._request_generator())

        # Compute total simulated test time for progress logging:
        sim_cfg = self.config.get("simulation", {})
        drain_ms = int(sim_cfg.get("drain_ms", 10_000))
        total_sim_ms = int(self.total_time_ms) if self.total_time_ms is not None else (self.duration_ms + drain_ms)

        # Start progress logger process that prints a line every simulated minute
        self.env.process(self._progress_logger(total_sim_ms, interval_ms=60_000))

        # Run according to configured timing policy
        if self.total_time_ms is not None:
            # Hard stop the simulation at total_time_ms
            self.env.run(until=self.total_time_ms)
        else:
            # Legacy behavior: run generation for duration_ms, then drain for configured time
            self.env.run(until=self.duration_ms)
            self.env.run(until=self.env.now + drain_ms)

        # Record summary timing
        self.metrics.sim_time_ms = int(self.env.now)
        self.metrics.wall_runtime_seconds = time.time() - self._wall_start

        # Build report
        report = self.metrics.build_report()
        return report


# -----------------------------
# CLI and config
# -----------------------------
DEFAULT_CONFIG = {
    "simulation": {
        "seed": 42,
        "duration_ms": 120000,
        "drain_ms": 10000,
    },
    "traffic": {
        "arrival": {"distribution": "poisson", "rate_per_sec": 20.0, "ramp": {"mode": "none"}},
        "request_duration_ms": {"mode": "distribution", "distribution": "lognormal", "mean_ms": 100.0, "sigma": 0.5},
        "cpu_demand": {"mode": "distribution", "distribution": "normal", "mean": 10.0, "stddev": 2.0},
    },
    "load_balancer": {
        "architecture": "round_robin",
        "hosts": 2,
        "rr": {
            "retry_limit": 2,
            "sync_delay_ms": 100,
            "idle_scale_down_threshold_requests_5m": 2,
        },
        "lc": {
            "shared_map_lock_latency_ms": 0,
            "idle_scale_down_threshold_outstanding_5m": 1,
        },
    },
    "targets": {
        "initial_count": 2,
        "max_concurrency_per_target": 10,
        "cpu_capacity_per_target": 100.0,
    },
    "scaling": {
        "min_targets": 1,
        "max_targets": 100,
        "scale_up_delay_ms": 200,
        "scale_down_delay_ms": 200,
    },
    "sampling": {
        "mode": "event",
        "sample_on": ["lb_decision"]
    },
    "reporting": {
        "output_dir": "reports",
        "writers": ["json", "markdown", "html"]
    },
}


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path:
        with open(path, "r") as f:
            user_cfg = json.load(f)
        # Shallow merge for simplicity; nested dicts merged at one level
        def merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
            out = dict(a)
            for k, v in b.items():
                if isinstance(v, dict) and isinstance(out.get(k), dict):
                    out[k] = merge(out[k], v)
                else:
                    out[k] = v
            return out
        cfg = merge(cfg, user_cfg)
    return cfg


def _build_html_report(report: Dict[str, Any], metrics: "MetricsCollector", title: str = "yolo-router Simulation Report") -> str:
    import json as _json
    # Summary
    summary = report.get("summary", {})
    sim_time_ms = summary.get("sim_time_ms", 0)
    total_requests = summary.get("total_requests", 0)
    scale_up_events = summary.get("scale_up_events", 0)
    scale_down_events = summary.get("scale_down_events", 0)
    wall_runtime_seconds = summary.get("wall_runtime_seconds", None)
    wall_runtime_display = "n/a" if wall_runtime_seconds is None else f"{wall_runtime_seconds:.3f}"

    # Retries histogram (exclude zero-retries bucket for histogram)
    retries = report.get("retries_histogram", {})
    # Sort keys numerically and filter out the 0 bucket
    keys_sorted = sorted(list(retries.keys()), key=lambda k: int(k))
    filtered_keys = [k for k in keys_sorted if int(k) != 0]
    retries_x = [str(k) for k in filtered_keys]
    retries_y = [retries[k] for k in filtered_keys]

    # Fleet size over time from decision events (downsample if large)
    times = [e.get("t_ms", 0) for e in metrics.decision_events]
    fleet = [e.get("fleet_size", 0) for e in metrics.decision_events]
    if len(times) > 5000:
        step = max(1, len(times) // 5000)
        times = times[::step]
        fleet = fleet[::step]

    # Distributions (limit for size)
    latencies = metrics.latencies[:5000]
    durations = metrics.service_durations[:5000]

    # Per-target totals from aggregated report
    per_target = report.get("per_target_metrics", {})
    pt_ids = list(per_target.keys())
    pt_totals = [per_target[k].get("total_requests", 0) for k in pt_ids]

    # Request rate over time (bucketed)
    sim_total_ms = int(sim_time_ms) if sim_time_ms else (times[-1] if times else 0)
    arrivals = getattr(metrics, "arrival_times_ms", [])
    bucket_ms = max(100, sim_total_ms // 500) if sim_total_ms > 0 else 1000
    if arrivals:
        max_t = max(arrivals)
        sim_total_ms = max(sim_total_ms, max_t)
        num_buckets = int(sim_total_ms // bucket_ms) + 1
        rate_counts = [0] * num_buckets
        for t in arrivals:
            idx = int(t // bucket_ms)
            if 0 <= idx < num_buckets:
                rate_counts[idx] += 1
        rate_times = [i * bucket_ms for i in range(num_buckets)]
        rate_rps = [c * (1000.0 / bucket_ms) for c in rate_counts]
    else:
        rate_times = []
        rate_rps = []

    # Retries over time (sum of retries_attempted across decision events per bucket)
    retries_times = []
    retries_rps = []
    decisions = metrics.decision_events
    if decisions and bucket_ms > 0:
        # build buckets same as rate_times
        num_buckets = int(sim_total_ms // bucket_ms) + 1 if sim_total_ms > 0 else 0
        if num_buckets > 0:
            retries_counts = [0] * num_buckets
            for e in decisions:
                t = int(e.get("t_ms", 0))
                r = int(e.get("retries_attempted", 0))
                idx = int(t // bucket_ms)
                if 0 <= idx < num_buckets:
                    retries_counts[idx] += r
            retries_times = [i * bucket_ms for i in range(num_buckets)]
            retries_rps = [c * (1000.0 / bucket_ms) for c in retries_counts]

    # Latency over time (completion times)
    lat_times = getattr(metrics, "latency_times_ms", [])[:]
    lat_values = getattr(metrics, "latency_values_ms", [])[:]
    # Keep a full copy for percentile calculations (do not downsample the source used for percentile windows)
    lat_times_full = getattr(metrics, "latency_times_ms", [])[:]
    lat_values_full = getattr(metrics, "latency_values_ms", [])[:]
    if len(lat_times) > 5000:
        step = max(1, len(lat_times) // 5000)
        lat_times = lat_times[::step]
        lat_values = lat_values[::step]

    # Compute trailing-window latency percentiles (p99, p90, p80) per time point
    # Use time axis = rate_times if available, otherwise fall back to decision event times
    time_points = rate_times if rate_times else times
    p99_series = []
    p90_series = []
    p80_series = []
    window_ms = 60_000  # trailing 1 minute
    if time_points and lat_times_full and lat_values_full:
        # Convert to arrays for faster selection if large
        lt = np.array(lat_times_full, dtype=float)
        lv = np.array(lat_values_full, dtype=float)
        for tp in time_points:
            start = tp - window_ms
            # select values with start < t <= tp
            mask = (lt > start) & (lt <= tp)
            vals = lv[mask]
            if vals.size:
                p99_series.append(float(np.percentile(vals, 99)))
                p90_series.append(float(np.percentile(vals, 90)))
                p80_series.append(float(np.percentile(vals, 80)))
            else:
                p99_series.append(None)
                p90_series.append(None)
                p80_series.append(None)
    else:
        p99_series = []
        p90_series = []
        p80_series = []

    # Helper to render a dict as a simple HTML table (one header row, one value row)
    def dict_to_table_html(d: Dict[str, Any], title: Optional[str] = None) -> str:
        if not d:
            return "" if title is None else f"<h3>{title}</h3><p><em>No data</em></p>"
        keys = list(d.keys())
        vals = [d[k] for k in keys]
        header = "".join(f"<th>{str(k)}</th>" for k in keys)
        row = "".join(f"<td>{str(v)}</td>" for v in vals)
        title_html = "" if title is None else f"<h3>{title}</h3>"
        return f"""{title_html}
<table class="table">
  <thead><tr>{header}</tr></thead>
  <tbody><tr>{row}</tr></tbody>
</table>
"""

    # Build numeric tables mirroring JSON numeric sections (non per-target)
    retries_tbl = dict_to_table_html(retries, "Retries Histogram")
    conc_tbl = dict_to_table_html(report.get("per_target_concurrency_percentiles", {}), "Per-Target Concurrent Requests (percentiles)")
    cpu_tbl = dict_to_table_html(report.get("per_target_cpu_util_percentiles", {}), "Per-Target CPU Utilization (percentiles)")
    fleet_tbl = dict_to_table_html(report.get("fleet_size_percentiles", {}), "Fleet Size (percentiles)")
    dur_tbl = dict_to_table_html(report.get("request_duration_percentiles_ms", {}), "Request Duration (ms) (percentiles)")
    lat_tbl = dict_to_table_html(report.get("request_latency_percentiles_ms", {}), "Request Latency (ms) (percentiles)")
    samples_tbl = dict_to_table_html(report.get("samples", {}), "Sample Counts")

    numeric_tables_html = f"""
<div class="section">
  <h2>Numeric Tables</h2>
  {retries_tbl}
  {conc_tbl}
  {cpu_tbl}
  {fleet_tbl}
  {dur_tbl}
  {lat_tbl}
  {samples_tbl}
</div>
"""

    # Build per-target tables
    # Render each target with its totals and percentiles
    def per_target_section(tid: str, metrics_obj: Dict[str, Any]) -> str:
        sec = [f'<div class="section"><h3>Target {tid}</h3>']
        sec.append(dict_to_table_html({"total_requests": metrics_obj.get("total_requests", 0)}))
        sec.append(dict_to_table_html(metrics_obj.get("concurrency_percentiles", {}), "Concurrency percentiles"))
        sec.append(dict_to_table_html(metrics_obj.get("cpu_util_percentiles", {}), "CPU utilization percentiles"))
        sec.append(dict_to_table_html(metrics_obj.get("request_duration_percentiles_ms", {}), "Request duration (ms) percentiles"))
        sec.append(dict_to_table_html(metrics_obj.get("request_latency_percentiles_ms", {}), "Request latency (ms) percentiles"))
        sec.append("</div>")
        return "\n".join(sec)

    if pt_ids:
        # sort by numeric when possible
        try:
            pt_ids_sorted = sorted(pt_ids, key=lambda x: int(x))
        except Exception:
            pt_ids_sorted = sorted(pt_ids)
    else:
        pt_ids_sorted = []

    per_target_tables_html = ""
    if pt_ids_sorted:
        sections = []
        sections.append('<div class="section"><h2>Per-Target Metrics (Tables)</h2>')
        for tid in pt_ids_sorted:
            sections.append(per_target_section(tid, per_target.get(tid, {})))
        sections.append("</div>")
        per_target_tables_html = "\n".join(sections)
    else:
        per_target_tables_html = ""

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>{title}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
body {{ font-family: Arial, sans-serif; margin: 20px; }}
h1, h2 {{ margin: 0.5em 0; }}
.chart {{ width: 100%; height: 420px; }}
.section {{ margin-bottom: 40px; }}

/* Table styling: borders, padding and improved spacing */
table.table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
table.table th, table.table td {{ border: 1px solid #ddd; padding: 10px 12px; text-align: left; vertical-align: middle; }}
table.table thead th {{ background-color: #f8f9fa; font-weight: 600; }}
table.table tbody tr:nth-child(even) {{ background-color: #fafafa; }}
.table-container {{ overflow-x: auto; padding: 8px 0; }}
</style>
</head>
<body>
<h1>{title}</h1>

<div class="section">
  <h2>Run Summary</h2>
  <table class="table">
    <thead>
      <tr>
        <th>Sim Time (ms)</th>
        <th>Total Requests</th>
        <th>Scale Ups</th>
        <th>Scale Downs</th>
        <th>Wall Runtime (s)</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>{sim_time_ms}</td>
        <td>{total_requests}</td>
        <td>{scale_up_events}</td>
        <td>{scale_down_events}</td>
        <td>{wall_runtime_display}</td>
      </tr>
    </tbody>
  </table>
</div>

<div class="section">
  <h2>Retries Histogram</h2>
  <div id="retries" class="chart"></div>
</div>

<div class="section">
  <h2>Fleet Size Over Time</h2>
  <div id="fleet" class="chart"></div>
</div>

<div class="section">
  <h2>Latency Distribution (samples)</h2>
  <div id="latency" class="chart"></div>
</div>

<div class="section">
  <h2>Service Duration Distribution (samples)</h2>
  <div id="duration" class="chart"></div>
</div>

<div class="section">
  <h2>Per-Target Total Requests</h2>
  <div id="perTargetTotals" class="chart"></div>
</div>

<div class="section">
  <h2>Retries Over Time</h2>
  <div id="retriesOverTime" class="chart"></div>
</div>

<div class="section">
  <h2>Request Rate Over Time</h2>
  <div id="reqRate" class="chart"></div>
</div>

<div class="section">
  <h2>Latency Over Time</h2>
  <div id="latencyOverTime" class="chart"></div>
</div>

{numeric_tables_html}

{per_target_tables_html}

<script>
const retriesX = {_json.dumps(retries_x)};
const retriesY = {_json.dumps(retries_y)};

const times = {_json.dumps(times)};
const fleet = {_json.dumps(fleet)};

const latencies = {_json.dumps(latencies)};
const durations = {_json.dumps(durations)};

const ptIds = {_json.dumps(pt_ids)};
const ptTotals = {_json.dumps(pt_totals)};

const rateTimes = {_json.dumps(rate_times)};
const rateRps = {_json.dumps(rate_rps)};
const retriesTimes = {_json.dumps(retries_times)};
const retriesRps = {_json.dumps(retries_rps)};
const latTimes = {_json.dumps(lat_times)};
const latValues = {_json.dumps(lat_values)};

const p99Series = {_json.dumps(p99_series)};
const p90Series = {_json.dumps(p90_series)};
const p80Series = {_json.dumps(p80_series)};

Plotly.newPlot('retries', [{{
  x: retriesX, y: retriesY, type: 'bar', marker: {{color: '#2a9d8f'}}
}}], {{title: 'Retries', xaxis: {{title: 'Retries'}}, yaxis: {{title: 'Count'}}}});

Plotly.newPlot('fleet', [{{
  x: times, y: fleet, type: 'scatter', mode: 'lines', line: {{color: '#264653'}}
}}], {{title: 'Fleet Size Over Time', xaxis: {{title: 't (ms)'}}, yaxis: {{title: '# targets'}}}});

Plotly.newPlot('latency', [{{
  x: latencies, type: 'histogram', marker: {{color: '#e76f51'}}, nbinsx: 50
}}], {{title: 'Latency (ms)', xaxis: {{title: 'Latency (ms)'}}, yaxis: {{title: 'Count'}}}});

Plotly.newPlot('duration', [{{
  x: durations, type: 'histogram', marker: {{color: '#f4a261'}}, nbinsx: 50
}}], {{title: 'Service Duration (ms)', xaxis: {{title: 'Duration (ms)'}}, yaxis: {{title: 'Count'}}}});

Plotly.newPlot('perTargetTotals', [{{
  x: ptIds, y: ptTotals, type: 'bar', marker: {{color: '#457b9d'}}
}}], {{title: 'Per-Target Total Requests', xaxis: {{title: 'Target ID'}}, yaxis: {{title: 'Total'}}}});

Plotly.newPlot('reqRate', [{{
  x: rateTimes, y: rateRps, type: 'scatter', mode: 'lines', line: {{color: '#1d3557'}}
}}], {{title: 'Request Rate (req/s) Over Time', xaxis: {{title: 't (ms)'}}, yaxis: {{title: 'req/s'}}}});

Plotly.newPlot('latencyOverTime', [
{{"x": latTimes, "y": latValues, "type": "scatter", "mode": "markers", "marker": {{ "color": "#8e44ad", "size": 3, "opacity": 0.5 }}, "name": "per-request latency"}} ,
{{"x": rateTimes, "y": p99Series, "type": "scatter", "mode": "lines", "line": {{ "color": "#d00000", "width": 2 }}, "name": "p99 (trailing 1m)"}} ,
{{"x": rateTimes, "y": p90Series, "type": "scatter", "mode": "lines", "line": {{ "color": "#f77f00", "width": 2 }}, "name": "p90 (trailing 1m)"}} ,
{{"x": rateTimes, "y": p80Series, "type": "scatter", "mode": "lines", "line": {{ "color": "#f4a261", "width": 2 }}, "name": "p80 (trailing 1m)"}} 
], {{title: 'Latency Over Time (per-request + trailing percentiles)', xaxis: {{title: 't (ms)'}}, yaxis: {{title: 'latency (ms)'}}}});

Plotly.newPlot('retriesOverTime', [{{
  x: retriesTimes, y: retriesRps, type: 'bar', marker: {{color: '#e63946'}}
}}], {{title: 'Retries Over Time (retries/sec)', xaxis: {{title: 't (ms)'}}, yaxis: {{title: 'retries/s'}}}});
</script>
</body>
</html>
"""
    return html

def write_reports(report: Dict[str, Any], config: Dict[str, Any], metrics: "MetricsCollector") -> None:
    rep_cfg = config.get("reporting", {})
    writers = rep_cfg.get("writers", ["json", "markdown"])
    outdir = rep_cfg.get("output_dir", "reports")
    ensure_dir(outdir)
    # JSON
    if "json" in writers:
        path = os.path.join(outdir, "report.json")
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Wrote JSON report: {path}")
    # Markdown
    if "markdown" in writers:
        md = MetricsCollector().report_markdown(report)  # using helper statically
        # Slight tweak: we need an instance method; create temp instance to reuse utility
        # Inlined above by creating temp instance
        path = os.path.join(outdir, "report.md")
        with open(path, "w") as f:
            f.write(md)
        print(f"Wrote Markdown report: {path}")
    # HTML
    if "html" in writers:
        html_path = os.path.join(outdir, "report.html")
        html = _build_html_report(report, metrics, title="yolo-router Simulation Report")
        with open(html_path, "w") as f:
            f.write(html)
        print(f"Wrote HTML report: {html_path}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="yolo-router simulation")
    parser.add_argument("--config", type=str, help="Path to JSON config", default=None)
    parser.add_argument("--arch", type=str, choices=["round_robin", "least_conns"], help="Override architecture")
    parser.add_argument("--seed", type=int, help="Override RNG seed")
    parser.add_argument("--output-dir", type=str, help="Override report output dir")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.arch:
        cfg.setdefault("load_balancer", {})["architecture"] = args.arch
    if args.seed is not None:
        cfg.setdefault("simulation", {})["seed"] = int(args.seed)
    if args.output_dir:
        cfg.setdefault("reporting", {})["output_dir"] = args.output_dir

    sim = Simulation(cfg)
    report = sim.run()
    write_reports(report, cfg, sim.metrics)

    # Also print a concise summary
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
