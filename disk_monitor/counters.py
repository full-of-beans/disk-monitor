"""Linux block-stat snapshots and interval metrics."""

import time
from dataclasses import dataclass

from disk_monitor.discovery import SYS_BLOCK, Generation

IN_PROGRESS = "io_in_progress"
FIELDS = (
    "reads_completed",
    "reads_merged",
    "sectors_read",
    "read_ms",
    "writes_completed",
    "writes_merged",
    "sectors_written",
    "write_ms",
    IN_PROGRESS,
    "io_ms",
    "weighted_io_ms",
    "discards_completed",
    "discards_merged",
    "sectors_discarded",
    "discard_ms",
    "flushes_completed",
    "flush_ms",
)

# Field, conversion factor from the counter delta per elapsed second.
METRICS = {
    "busy_percent": ("io_ms", 0.1),
    "read_time_percent": ("read_ms", 0.1),
    "write_time_percent": ("write_ms", 0.1),
    "read_bytes_per_second": ("sectors_read", 512),
    "write_bytes_per_second": ("sectors_written", 512),
    "read_iops": ("reads_completed", 1),
    "write_iops": ("writes_completed", 1),
}


@dataclass(frozen=True)
class Sample:
    generation: Generation
    monotonic_ns: int
    counters: dict[str, int]


def sample(device, sys_block=SYS_BLOCK, clock=time.monotonic_ns):
    fields = (sys_block / device.name / "stat").read_text().split()
    stamp = clock()
    if (len(fields) not in (11, 15) and len(fields) < 17) or any(
        not f.isascii() or not f.isdecimal() for f in fields
    ):
        raise ValueError("invalid kernel block stat")
    return Sample(device.generation, stamp, dict(zip(FIELDS, map(int, fields))))


def measure_io(previous, current, previous_idle_ns, max_idle_gap_ns):
    """Return the seven interval metrics and the current idle duration in ns."""
    empty = dict.fromkeys(METRICS)
    if previous is None or current is None or current.generation != previous.generation:
        return empty, None
    elapsed_ns = current.monotonic_ns - previous.monotonic_ns
    if elapsed_ns <= 0:
        return empty, None
    deltas = {
        field: value - previous.counters[field]
        for field, value in current.counters.items()
        if field != IN_PROGRESS and field in previous.counters
    }
    if any(delta < 0 for delta in deltas.values()):
        return empty, None
    elapsed = elapsed_ns / 1_000_000_000
    rates = {
        name: round(deltas[field] * scale / elapsed, 3)
        for name, (field, scale) in METRICS.items()
    }
    if (
        elapsed_ns > max_idle_gap_ns
        or current.counters.keys() != previous.counters.keys()
    ):
        return rates, None
    if (
        previous.counters[IN_PROGRESS]
        or current.counters[IN_PROGRESS]
        or any(deltas.values())
    ):
        return rates, 0
    return rates, (previous_idle_ns or 0) + elapsed_ns
