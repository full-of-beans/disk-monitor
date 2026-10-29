"""Independent deadlines; one hardware probe per drive, I/O never waits for SMART."""

import fcntl
import os
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Full, Queue

from disk_monitor.discovery import discover, resolve, same_generation
from disk_monitor.probes import io_counter, observe
from disk_monitor.state import SCHEMA_VERSION, publish, with_freshness

SOURCES = {
    "io": "sysfs:stat:io_ticks",
    "spindle": "hdparm:-C",
    "temperature": "smartctl:scttempsts",
    "self_test": "smartctl:capabilities",
}


def empty(interval, kind):
    return ({"activity": "unknown"} if kind == "io" else {}) | {
        "value": "unknown" if kind in ("spindle", "self_test") else None,
        "attempted_at": None,
        "observed_at": None,
        "last_value": None,
        "last_success_at": None,
        "error": "not_observed",
        "max_age_seconds": max(2 * interval, interval + 15),
        "source": SOURCES[kind],
    }


class Monitor:
    def __init__(
        self,
        config,
        *,
        discoverer=discover,
        probe=observe,
        counter=io_counter,
        clock=time.monotonic,
        epoch=time.time,
        validator=same_generation,
    ):
        self.config, self.discoverer, self.probe, self.counter = (
            config,
            discoverer,
            probe,
            counter,
        )
        self.clock, self.epoch, self.validator = clock, epoch, validator
        self.next_discovery = 0
        self.dirty = True
        # Consumers drain this bounded mailbox in their own worker. No callback
        # or transport ever runs in the scheduler. Oldest events may be dropped.
        self.completions = Queue(maxsize=32)
        self.pool = ThreadPoolExecutor(max_workers=len(config.drives))
        self.pending, self.devices, self.baselines = {}, {}, {}
        self.due = {(d.serial, k): 0 for d in config.drives for k in config.intervals}
        self.rows = {
            d.serial: {
                "serial": d.serial,
                "bay": d.bay,
                "presence": "unknown",
                "last_active_at": None,
                "device": None,
                "presence_observed_at": None,
                "observations": {k: empty(i, k) for k, i in config.intervals.items()},
            }
            for d in config.drives
        }

    def inventory(self):
        try:
            return resolve(self.config.drives, self.discoverer())
        except (OSError, UnicodeError):
            return {d.serial: ("unknown", None) for d in self.config.drives}

    def snapshot(self):
        return with_freshness(
            {
                "schema_version": SCHEMA_VERSION,
                "published_at": self.epoch(),
                "disks": self.rows,
            },
            self.epoch(),
        )

    def notify(self, serial, kind, stamp):
        event = {
            "serial": serial,
            "kind": kind,
            "completed_at": stamp,
            "state": self.snapshot(),
        }
        try:
            self.completions.put_nowait(event)
        except Full:
            try:
                self.completions.get_nowait()
            except Empty:
                pass  # A consumer drained it concurrently.
            self.completions.put_nowait(event)

    def record(self, serial, kind, value, error, extra=None, stamp=None):
        drive = self.rows[serial]
        row = drive["observations"][kind]
        now = self.epoch() if stamp is None else stamp
        old_activity = row.get("activity", "unknown")
        row.update(
            value=("unknown" if kind in ("spindle", "self_test") and error else value),
            attempted_at=now,
            observed_at=now if error is None else None,
            error=error,
        )
        if error is None:
            row.update(last_value=value, last_success_at=now)
        if kind == "spindle":
            row["raw_state"] = (extra or {}).get("raw_state")
        if kind == "io":
            row["window_seconds"] = (extra or {}).get("window_seconds")
            row["activity"] = (
                "unknown" if error else "busy" if (extra or {}).get("busy") else "idle"
            )
            self.dirty |= row["activity"] != old_activity
        else:
            # Every completed hardware attempt is meaningful, including failures.
            self.dirty = True
        active = error is None and (
            kind == "spindle"
            and value == "active"
            and (extra or {}).get("raw_state")
            in {"active/idle", "active", "idle", "idle_a", "idle_b", "idle_c"}
            or kind == "io"
            and row["activity"] == "busy"
            or kind in ("temperature", "self_test")
            and (extra or {}).get("confirms_active") is True
        )
        if active:
            prior = drive["last_active_at"]
            drive["last_active_at"] = max(prior, now) if prior is not None else now
        self.notify(serial, kind, now)

    def hardware(self, serial, device, kind):
        # Full metadata validation is deliberately retained at hardware boundaries.
        # It also detects duplicate identity claims that a diskseq check cannot.
        if self.inventory().get(serial) != ("present", device):
            return None, "identity_changed", {}, self.epoch()
        value, error, extra = self.probe(kind, device)
        stamp = self.epoch()
        if self.inventory().get(serial) != ("present", device):
            return None, "identity_changed", {}, stamp
        return value, error, extra, stamp

    def reconcile(self, inventory):
        for serial, (presence, device) in inventory.items():
            row = self.rows[serial]
            if device != self.devices.get(serial) or presence != row["presence"]:
                row["observations"] = {
                    k: empty(i, k) for k, i in self.config.intervals.items()
                }
                self.baselines.pop(serial, None)
                for kind in self.config.intervals:
                    self.due[serial, kind] = 0
                self.dirty = True
            self.devices[serial] = device
            row.update(
                presence=presence,
                device=device.path if device else None,
                presence_observed_at=self.epoch(),
                device_generation=list(device.generation) if device else None,
            )

    def tick(self):
        if self.clock() >= self.next_discovery:
            self.reconcile(self.inventory())
            self.next_discovery = self.clock() + self.config.discovery_interval
        completed = [
            (serial, task) for serial, task in self.pending.items() if task[0].done()
        ]
        if completed:
            # Validate against live metadata, not the periodic inventory cache.
            self.reconcile(self.inventory())
        for serial, (future, device, kind) in completed:
            value, error, extra, stamp = future.result()
            if self.devices.get(serial) == device:
                if error == "identity_changed":
                    self.reconcile({serial: ("unknown", None)})
                    self.next_discovery = 0
                else:
                    self.record(serial, kind, value, error, extra, stamp)
                    self.due[serial, kind] = self.clock() + self.config.intervals[kind]
            del self.pending[serial]
        for serial, device in list(self.devices.items()):
            if device is None:
                continue
            now = self.clock()
            if now >= self.due[serial, "io"]:
                # Guard both sides: a reused sdX must never contribute counters
                # to this serial's baseline or last_active_at.
                if not self.validator(device):
                    self.reconcile({serial: ("unknown", None)})
                    self.next_discovery = 0
                    continue
                try:
                    ticks, counters = self.counter(device)
                    if not self.validator(device):
                        self.reconcile({serial: ("unknown", None)})
                        self.next_discovery = 0
                        continue
                    prior = self.baselines.get(serial)
                    value, error, busy = None, "baseline_required", False
                    if (
                        prior
                        and 0 < now - prior[0] <= 2 * self.config.intervals["io"]
                        and ticks >= prior[1]
                        and len(counters) == len(prior[2])
                        and all(a >= b for a, b in zip(counters, prior[2]))
                    ):
                        value = min(100.0, (ticks - prior[1]) / ((now - prior[0]) * 10))
                        busy = ticks > prior[1] or counters != prior[2]
                        error = None
                    self.baselines[serial] = (now, ticks, counters)
                    self.record(
                        serial,
                        "io",
                        value,
                        error,
                        {
                            "window_seconds": now - prior[0] if error is None else None,
                            "busy": busy,
                        },
                    )
                except (OSError, ValueError, UnicodeError):
                    self.baselines.pop(serial, None)
                    self.record(serial, "io", None, "counters_unavailable")
                self.due[serial, "io"] = now + self.config.intervals["io"]
            # Oldest due hardware observation wins; no overlapping commands on a drive.
            if serial not in self.pending:
                kinds = [
                    k
                    for k in self.config.intervals
                    if k != "io" and now >= self.due[serial, k]
                ]
                if kinds:
                    kind = min(kinds, key=lambda k: self.due[serial, k])
                    self.pending[serial] = (
                        self.pool.submit(self.hardware, serial, device, kind),
                        device,
                        kind,
                    )
        return self.snapshot()

    def publish_changes(self):
        if self.dirty:
            publish(self.config.output, self.snapshot())
            self.dirty = False

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


def serve(config, stop):
    # Lifetime lock covers foreground and systemd executions using this output.
    config.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        str(config.output) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monitor = Monitor(config)
        try:
            while not stop.is_set():
                monitor.tick()
                monitor.publish_changes()
                stop.wait(1)
        finally:
            monitor.close()
            # Readers expire each measurement; file age is not service health.
