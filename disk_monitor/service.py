"""Independent deadlines; one hardware probe per drive, I/O never waits for SMART."""

import fcntl
import os
import time
from concurrent.futures import ThreadPoolExecutor

from disk_monitor.discovery import discover, resolve
from disk_monitor.probes import io_counter, observe
from disk_monitor.state import SCHEMA_VERSION, publish, with_freshness

SOURCES = {
    "io": "sysfs:stat:io_ticks",
    "spindle": "hdparm:-C",
    "temperature": "smartctl:scttempsts",
    "self_test": "smartctl:capabilities",
}


def empty(interval, kind):
    return {
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
    ):
        self.config, self.discoverer, self.probe, self.counter = (
            config,
            discoverer,
            probe,
            counter,
        )
        self.clock, self.epoch = clock, epoch
        self.pool = ThreadPoolExecutor(max_workers=len(config.drives))
        self.pending, self.devices, self.baselines = {}, {}, {}
        self.due = {(d.serial, k): 0 for d in config.drives for k in config.intervals}
        self.rows = {
            d.serial: {
                "serial": d.serial,
                "bay": d.bay,
                "presence": "unknown",
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

    def record(self, serial, kind, value, error, extra=None):
        row = self.rows[serial]["observations"][kind]
        now = self.epoch()
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
        if extra:
            row.update(extra)

    def hardware(self, serial, device, kind):
        # Recheck metadata immediately before issuing a command, then check again
        # on completion. Hot unplug can still race any userspace device open.
        if self.inventory().get(serial) != ("present", device):
            return None, "identity_changed", {}
        return self.probe(kind, device)

    def tick(self):
        inventory = self.inventory()
        for serial, (presence, device) in inventory.items():
            row = self.rows[serial]
            old = self.devices.get(serial)
            if device != old or presence != row["presence"]:
                row["observations"] = {
                    k: empty(i, k) for k, i in self.config.intervals.items()
                }
                self.baselines.pop(serial, None)
                for kind in self.config.intervals:
                    self.due[serial, kind] = 0
            self.devices[serial] = device
            row.update(
                presence=presence,
                device=device.path if device else None,
                presence_observed_at=self.epoch(),
                device_generation=list(device.generation) if device else None,
            )
        # Collect without blocking other deadlines. Reject disconnected results.
        for serial, (future, device, kind) in list(self.pending.items()):
            if not future.done():
                continue
            value, error, extra = future.result()
            if self.devices.get(serial) == device:
                self.record(serial, kind, value, error, extra)
                self.due[serial, kind] = self.clock() + self.config.intervals[kind]
            del self.pending[serial]
        for serial, device in self.devices.items():
            if device is None:
                continue
            now = self.clock()
            if now >= self.due[serial, "io"]:
                try:
                    ticks = self.counter(device)
                    prior = self.baselines.get(serial)
                    value, error = None, "baseline_required"
                    if (
                        prior
                        and 0 < now - prior[0] <= 2 * self.config.intervals["io"]
                        and ticks >= prior[1]
                    ):
                        value = min(100.0, (ticks - prior[1]) / ((now - prior[0]) * 10))
                        error = None
                    self.baselines[serial] = (now, ticks)
                    self.record(
                        serial,
                        "io",
                        value,
                        error,
                        {"window_seconds": now - prior[0] if error is None else None},
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
        return with_freshness(
            {
                "schema_version": SCHEMA_VERSION,
                "published_at": self.epoch(),
                "disks": self.rows,
            },
            self.epoch(),
        )

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
                publish(config.output, monitor.tick())
                stop.wait(1)
        finally:
            monitor.close()
            # Leave evidence readable; readers must expire the heartbeat.
