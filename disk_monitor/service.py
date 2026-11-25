"""One monotonic monitoring cycle and two atomic snapshot outputs."""

import fcntl
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime

from disk_monitor.counters import measure_io, sample
from disk_monitor.discovery import discover, resolve, same_block_node, same_generation
from disk_monitor.hooks import Hooks
from disk_monitor.probes import Smart, observe
from disk_monitor.state import SCHEMA_VERSION, encode, publish

LOG = logging.getLogger(__name__)
SMART_WORKERS = 4


def utc_timestamp():
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Monitor:
    def __init__(
        self,
        config,
        stop,
        *,
        discoverer=discover,
        probe=observe,
        counter=sample,
        validator=same_generation,
        node_validator=same_block_node,
        timestamp=utc_timestamp,
    ):
        self.config = config
        self.stop = stop
        self.discoverer = discoverer
        self.probe = probe
        self.counter = counter
        self.validator = validator
        self.node_validator = node_validator
        self.timestamp = timestamp
        self.devices = {}
        self.baselines = {}
        self.max_idle_gap_ns = int(2 * config.interval * 1_000_000_000)
        self.pool = ThreadPoolExecutor(max_workers=SMART_WORKERS)
        self.hooks = Hooks(config.post_hooks)

    def inventory(self):
        try:
            return resolve(self.config.drives, self.discoverer())
        except (OSError, UnicodeError) as error:
            LOG.warning("disk discovery unavailable: %s", error)
            return {drive.serial: ("unknown", None) for drive in self.config.drives}

    def hardware(self, device):
        if self.stop.is_set():
            return Smart()
        if not self.validator(device):
            return Smart(error="identity_changed")
        if not self.node_validator(device):
            return Smart(error="SMART block node unavailable or mismatched")
        result = self.probe(device)
        if not self.validator(device):
            return Smart(error="identity_changed")
        if not self.node_validator(device):
            return Smart(error="SMART block node changed during probe")
        return result

    def cycle(self):
        self.devices = self.inventory()
        samples, targets = {}, {}
        for serial, (presence, device) in self.devices.items():
            if self.stop.is_set():
                return
            if presence != "present":
                continue
            if not self.validator(device):
                self.devices[serial] = ("unknown", None)
                continue
            try:
                current = self.counter(device)
                if not self.validator(device):
                    self.devices[serial] = ("unknown", None)
                    continue
                samples[serial] = current
            except (OSError, ValueError) as error:
                LOG.warning("%s: kernel counters unavailable: %s", serial, error)
            targets[serial] = device

        smart = dict(zip(targets, self.pool.map(self.hardware, targets.values())))
        if self.stop.is_set():
            return
        # Recheck claims after all workers finish, including new duplicate identities.
        if targets:
            confirmed = self.inventory()
            for serial, device in targets.items():
                if (
                    confirmed[serial] != ("present", device)
                    or smart[serial].error == "identity_changed"
                ):
                    presence, _ = confirmed[serial]
                    self.devices[serial] = (
                        "unknown" if presence == "present" else presence,
                        None,
                    )
                    samples.pop(serial, None)
                    smart.pop(serial)
                    LOG.warning("%s: identity changed during monitoring", serial)

        disks, raw, baselines = {}, {}, {}
        for serial, (presence, device) in self.devices.items():
            observation = smart.get(serial, Smart())
            if observation.error:
                LOG.warning("%s: %s", serial, observation.error)
            elif presence == "unknown" and device:
                LOG.warning(
                    "%s: supported ATA SMART transport could not be established", serial
                )
            elif presence == "ambiguous":
                LOG.warning("%s: multiple identity claims", serial)
            current = samples.get(serial)
            previous, previous_idle_ns, previous_ata_serial = self.baselines.get(
                serial, (None, None, None)
            )
            rates, idle_ns = measure_io(
                previous, current, previous_idle_ns, self.max_idle_gap_ns
            )
            if current is not None and device.ata_serial != previous_ata_serial:
                idle_ns = None
            idle_seconds = None if idle_ns is None else idle_ns // 1_000_000_000
            disks[serial] = {
                "device": device.path if device else None,
                "presence": presence,
                "spindle": observation.spindle,
                "temperature_c": observation.temperature_c,
                "self_test": observation.self_test,
                "io": {**rates, "idle_seconds": idle_seconds},
            }
            if current is not None:
                baselines[serial] = current, idle_ns, device.ata_serial
                raw[serial] = {
                    "device": device.path,
                    "diskseq": current.generation.diskseq,
                    "major_minor": current.generation.major_minor,
                    "sysfs_path": current.generation.sysfs_path,
                    "sysfs_inode": current.generation.sysfs_inode,
                    "sample_monotonic_ns": current.monotonic_ns,
                    "counters": current.counters,
                }
        self.baselines = baselines
        stamp = self.timestamp()
        payload = encode(
            {"schema_version": SCHEMA_VERSION, "observed_at": stamp, "disks": disks}
        )
        try:
            publish(self.config.counters, encode({"updated_at": stamp, "disks": raw}))
            publish(self.config.output, payload)
        except OSError as error:
            LOG.error("snapshot publication failed: %s", error)
        else:
            if not self.stop.is_set():
                self.hooks.dispatch(payload)

    def close(self):
        self.hooks.close()
        self.pool.shutdown(wait=True, cancel_futures=True)


def schedule(cycle, interval, stop, clock=time.monotonic_ns):
    period = round(interval * 1_000_000_000)
    deadline = clock()
    while not stop.is_set():
        cycle()
        deadline += period
        now = clock()
        if now > deadline:
            missed = (now - deadline + period - 1) // period
            deadline += missed * period
            LOG.warning("monitor cycle overran, skipping %d scheduled start(s)", missed)
        stop.wait(max(0, deadline - now) / 1_000_000_000)


def serve(config, stop):
    # Lock both names: different configurations may share either output.
    paths = {
        path.parent.resolve() / path.name for path in (config.output, config.counters)
    }
    if len(paths) != 2:
        raise ValueError("output paths refer to the same file")
    with ExitStack() as stack:
        for path in sorted(paths):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            descriptor = os.open(
                str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            lock = stack.enter_context(os.fdopen(descriptor, "r+"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OSError(f"another disk-monitor writer owns {path}") from error
        monitor = Monitor(config, stop)
        try:
            schedule(monitor.cycle, config.interval, stop)
        finally:
            monitor.close()
