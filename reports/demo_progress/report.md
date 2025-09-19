# yolo-router Simulation Report
## Run Summary
| sim_time_ms | total_requests | scale_up_events | scale_down_events | scale_up_targets_added | scale_down_targets_removed | wall_runtime_seconds |
| --- | --- | --- | --- | --- | --- | --- |
| 1200000 | 11855 | 2 | 0 | 2 | 0 | 0.2506866455078125 |
## Retries Histogram
| 0 |
| --- |
| 11855 |

## Per-Target Concurrent Requests (percentiles)
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 3.0 | 2.0 | 1.0 | 1.0 | 1.0 |

## Per-Target CPU Utilization (percentiles)
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 0.3193896334415172 | 0.19345259846729973 | 0.11862761979098244 | 0.10614800285457374 | 0.06123898129405408 |

## Fleet Size (percentiles)
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 4.0 | 4.0 | 4.0 | 4.0 | 4.0 |

## Request Duration (ms) (percentiles)
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 905.0 | 319.0 | 189.0 | 151.0 | 99.0 |

## Request Latency (ms) (percentiles)
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 905.0 | 319.0 | 189.0 | 151.0 | 99.0 |

## Sample Counts
| num_decision_events | num_fleet_samples | num_target_concurrency_samples | num_target_cpu_util_samples |
| --- | --- | --- | --- |
| 11855 | 11855 | 47419 | 47419 |

## Per-Target Metrics

### Target 1
| total_requests |
| --- |
| 2964 |

Concurrency percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 2.0 | 2.0 | 1.0 | 1.0 | 1.0 |

CPU utilization percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 0.270031674392863 | 0.1968618370745166 | 0.11822814046559622 | 0.10599479084701371 | 0.059611953456268664 |

Request duration (ms) percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 462.0 | 329.0 | 190.0 | 152.0 | 99.0 |

Request latency (ms) percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 462.0 | 329.0 | 190.0 | 152.0 | 99.0 |

### Target 2
| total_requests |
| --- |
| 2963 |

Concurrency percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 3.0 | 2.0 | 1.0 | 1.0 | 1.0 |

CPU utilization percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 0.3193896334415172 | 0.20060420361641526 | 0.11929212782590298 | 0.10627117000439495 | 0.06301713271675596 |

Request duration (ms) percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 905.0 | 324.7600000000002 | 190.0 | 153.0 | 99.0 |

Request latency (ms) percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 905.0 | 324.7600000000002 | 190.0 | 153.0 | 99.0 |

### Target 3
| total_requests |
| --- |
| 2964 |

Concurrency percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 2.0 | 2.0 | 1.0 | 1.0 | 1.0 |

CPU utilization percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 0.29217976277539476 | 0.19022204386080196 | 0.11862276734408228 | 0.10627898693449644 | 0.06337941231690548 |

Request duration (ms) percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 539.0 | 312.3699999999999 | 185.0 | 150.0 | 98.0 |

Request latency (ms) percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 539.0 | 312.3699999999999 | 185.0 | 150.0 | 98.0 |

### Target 4
| total_requests |
| --- |
| 2964 |

Concurrency percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 3.0 | 2.0 | 1.0 | 1.0 | 1.0 |

CPU utilization percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 0.30339869118519774 | 0.18545991943060655 | 0.11858684218067428 | 0.10612958080162224 | 0.057918347830056505 |

Request duration (ms) percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 569.0 | 301.3699999999999 | 188.0 | 151.0 | 99.0 |

Request latency (ms) percentiles
| p100 | p99 | p90 | p80 | p50 |
| --- | --- | --- | --- | --- |
| 569.0 | 301.3699999999999 | 188.0 | 151.0 | 99.5 |
