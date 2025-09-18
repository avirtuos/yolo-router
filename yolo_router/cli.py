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

    def record_request_completion(self, req: Request) -> None:
        self.retries_hist[req.retries_attempted] += 1
        if req.latency_ms is not None:
            self.latencies.append(float(req.latency_ms))
        if req.service_duration_ms is not None:
            self.service_durations.append(float(req.service_duration_ms))

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
            conc_vals.append(tgt.current_concurrency)
            cpu_vals.append(tgt.cpu_utilization())

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

        return {
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
        }

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
    ) -> None:
        self.env = env
        self.topology = topology
        self.max_concurrency_per_target = max_concurrency_per_target
        self.cpu_capacity_per_target = cpu_capacity_per_target
        self.scale_up_delay_ms = int(delays.get("scale_up_delay_ms", 0))
        self.scale_down_delay_ms = int(delays.get("scale_down_delay_ms", 0))
        self.min_targets = int(delays.get("min_targets", 1))
        self.max_targets = int(delays.get("max_targets", 1000000))

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
        return created_ids

    def _scale_down_proc(self, target_id: int, initiator_host_id: Optional[int]):
        yield self.env.timeout(self.scale_down_delay_ms)
        if self.topology.fleet_size() <= self.min_targets:
            return False
        self.topology.remove_target(target_id, initiator_host_id=initiator_host_id)
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
        self.seed = int(config.get("simulation", {}).get("seed", 42))
        self.duration_ms = int(config.get("simulation", {}).get("duration_ms", 60_000))
        self.rng = np.random.default_rng(self.seed)

        self.env = simpy.Environment()
        self.topology = Topology(self.env)
        targets_cfg = config.get("targets", {})
        self.max_concurrency_per_target = int(targets_cfg.get("max_concurrency_per_target", 10))
        self.cpu_capacity_per_target = float(targets_cfg.get("cpu_capacity_per_target", 100.0))

        self.scaler = ScalingService(
            env=self.env,
            topology=self.topology,
            max_concurrency_per_target=self.max_concurrency_per_target,
            cpu_capacity_per_target=self.cpu_capacity_per_target,
            delays=config.get("scaling", {}),
        )
        self.metrics = MetricsCollector()

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

    # --------------- Traffic generation ---------------
    def _sample_interarrival_ms(self, cfg: Dict[str, Any]) -> int:
        dist = cfg.get("distribution", "poisson")
        if dist == "poisson":
            rate_per_sec = float(cfg.get("rate_per_sec", 10.0))
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
            if now >= self.duration_ms:
                return
            # Generate request
            req = Request(
                id=next_id,
                arrival_time_ms=now,
                duration_ms=self._sample_duration_ms(dur_cfg),
                cpu_demand=self._sample_cpu_demand(cpu_cfg),
            )
            next_id += 1
            # Choose a host uniformly
            host = self.lb_hosts[self.rng.integers(0, len(self.lb_hosts))]
            # Spawn handling process
            self.env.process(host.handle_request(req))
            # Wait for next arrival
            inter_ms = self._sample_interarrival_ms(arrival_cfg)
            yield self.env.timeout(inter_ms)

    def run(self) -> Dict[str, Any]:
        # Start request generator
        self.env.process(self._request_generator())
        # Run until duration; allow inflight to finish by running a bit longer
        self.env.run(until=self.duration_ms)
        # Allow in-flight requests to complete; run additional time depending on max duration configured
        drain_ms = int(self.config.get("simulation", {}).get("drain_ms", 10_000))
        self.env.run(until=self.env.now + drain_ms)

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
        "arrival": {"distribution": "poisson", "rate_per_sec": 20.0},
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
        "writers": ["json", "markdown"]
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


def write_reports(report: Dict[str, Any], config: Dict[str, Any]) -> None:
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
    write_reports(report, cfg)

    # Also print a concise summary
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
