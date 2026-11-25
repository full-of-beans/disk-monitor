# Monitoring and telemetry contract

## Identity and transport

Inventory is refreshed at the start of each cycle. Linux sysfs identifies
whole rotational direct-access devices. Native libata topology and the ATA
vendor identify the `ata` transport. USB topology plus cached `ID_ATA=1`
identify `sat`. Cached udev `ID_SERIAL_SHORT` provides the ATA serial.
Native disks may also provide it through sysfs. Verified full by-id names
and cached identity representations can be configured as exact aliases.
Each discovery pass constructs a single reverse index from valid by-id
symlinks to whole devices and ignores partition links. Discovery issues no
firmware commands.

The registry uses serial as its key. Two matching devices or two configured
claims on one device invalidate that mapping. An unverified USB transport
remains unknown. The monitor checks the sysfs path, inode, major/minor and
`diskseq` across reads and probes, verifies the block node before and after
SMART, compares returned firmware serials, and rechecks inventory before
accepting results. With older kernels that lack `diskseq`, only the remaining
kernel identity is available. Userspace checks cannot eliminate the final
race between device verification and opening a device node. Bridges that
replace the physical drive behind an unchanged Linux device may also
conceal that change. Commission the topology and use the enclosure's
normal safe removal procedure.

## Cycle and publication

A monotonic schedule starts immediately. The configured interval sets
start opportunities. A cycle that overruns skips elapsed opportunities,
and cycles never overlap. The monitor reads one kernel snapshot per resolved
drive and runs up to four SMART subprocesses concurrently, each with a
three-second timeout. One failed counter read or SMART invocation leaves
the other observations available. Large inventories may exceed one
interval. The service stops scheduling inspections on shutdown and waits for
bounded in-flight work. Linux cannot reap a task that remains stuck in an
uninterruptible kernel operation until the driver returns.

A lifetime lock covers each output path. The monitor assigns a single UTC
completion time just before replacing `counters.json` and then `state.json`.
Each replacement is atomic for readers of that file. Match `updated_at` to
`observed_at` if comparing the two. The raw file can be one cycle ahead during
publication or following a processed-file write failure. A failed write is
logged, suppresses hook dispatch for that cycle, and leaves its previous file
intact. Continuous in-memory sampling proceeds on later cycles.

## On-demand refresh

`SIGUSR1` wakes the running scheduler. The main process runs the same
complete cycle for every start, with the same in-memory counter baselines,
idle duration, SMART guard, publication order, and post-hook dispatch.
Signal delivery is asynchronous and provides no per-request acknowledgment
or completion result.

- A refresh received while the scheduler is waiting normally starts a
  cycle promptly. If the next periodic deadline is due or within 100 ms,
  that regular cycle satisfies the request at its normal start.
- All requests pending when the scheduler takes a batch are coalesced.
  Requests arriving after that point, including during the cycle, are
  eligible for one follow-up cycle under the same deadline rule.
- Periodic deadlines stay anchored to the original monotonic schedule.
  After either kind of cycle, periodic deadlines strictly earlier than
  cycle completion are skipped. Cycles never overlap.
- `SIGINT` and `SIGTERM` wake the scheduler and take priority over pending
  refreshes. Existing shutdown handling covers in-flight work.

A refresh requested by a post-cycle hook can cause another delivery to
that hook. Request a refresh only for a new condition that needs it, and
ensure the hook can recognize the same condition on subsequent deliveries.

## Standby-protected SMART

The combined command is:

```sh
smartctl -d <ata-or-sat> -n standby,3,5 -i -c -l scttempsts --json=o <device>
```

The explicit transport avoids device-type autodetection. The `standby`
threshold stops the query in standby and sleep. ATA Idle, Idle_A, Idle_B
and Idle_C permit polling and count as `active` in telemetry. A poll can
bring a drive out of reduced spindle speed in Idle_C. The `-i` output
supplies ATA identity and the power-mode line after the power check.
Smartmontools 7.3 provides the capability self-test value and SCT current
temperature in JSON. `--json=o` retains the power-mode and diagnostic text.
There is no unprotected retry.

Exit 3 requires the explicit ATA standby diagnostic before it counts as
standby. Smartctl can report sleep when a power check fails, so that
diagnostic alone leaves power unknown. The second custom status, 5, tells
smartctl to stop if the power check is unsupported or returns an
unrecognized value. Unsupported, unconfirmed and conflicting power
evidence leaves SMART observations unknown. Other exit codes are bitmasks.
The parser rejects argument, open and identification errors and considers
independent structured observations even when another command fails. A
capability value indicating an ongoing self-test reports `active`. A
recognized completed/aborted status reports `inactive`. The self-test field
does not represent the last test's result or general drive health.

Smartctl ultimately relies on drive firmware and the USB bridge to implement
ATA CHECK POWER MODE truthfully and without waking media. Some devices
return an inaccurate active state, and power can change between the check
and later commands. Smartctl also recognizes obsolete NV-cache states for
which the standby threshold does not guarantee suppression. Keep hardware
with those behaviors out of the monitored inventory. Some HDDs omit SCT
temperature. Verify both polling while awake and no wake-up from standby
during [commissioning](validation.md).

References: [smartctl 7.3 manual](https://github.com/smartmontools/smartmontools/blob/RELEASE_7_3/smartmontools/smartctl.8.in)
and [7.3 ATA JSON and power handling](https://github.com/smartmontools/smartmontools/blob/RELEASE_7_3/smartmontools/ataprint.cpp).

## Processed snapshot

```json
{
  "schema_version": 4,
  "observed_at": "2026-11-25T15:00:00.000000Z",
  "disks": {
    "SERIAL_001": {
      "device": "/dev/sda",
      "presence": "present",
      "spindle": "active",
      "temperature_c": 34,
      "self_test": "inactive",
      "io": {
        "idle_seconds": 1860,
        "busy_percent": 0.0,
        "read_time_percent": 0.0,
        "write_time_percent": 0.0,
        "read_bytes_per_second": 0.0,
        "write_bytes_per_second": 0.0,
        "read_iops": 0.0,
        "write_iops": 0.0
      }
    }
  }
}
```

Every configured serial is present in `disks`. Presence is `present`,
`missing`, `ambiguous` or `unknown`. An unverified transport may retain a
device path for diagnosis while its observations remain unavailable.
Spindle uses `active`, `standby` or `unknown`. Self-test uses `active`,
`inactive` or `unknown`. Unavailable numbers are JSON nulls. Evidence is
current-cycle only. For example, a standby or timed-out probe has a null
temperature. Usable I/O remains available after a SMART timeout. The
completion time describes snapshot publication readiness. Counter reads
and firmware observations within the cycle can be separated by seconds.
Consumers should impose a snapshot age appropriate to their own use.

Schema 4 adds `io.idle_seconds` to each disk, including missing disks.
The seven rate and utilization fields use the same calculations as schema 3.
Post-hooks receive the schema 4 snapshot.

## Raw counter snapshot

```json
{
  "updated_at": "2026-11-25T15:00:00.000000Z",
  "disks": {
    "SERIAL_001": {
      "device": "/dev/sda",
      "diskseq": 12,
      "major_minor": "8:0",
      "sysfs_path": "/sys/devices/pci0000:00/0000:00:17.0/ata1/host0/target0:0:0/0:0:0:0/block/sda",
      "sysfs_inode": 9912,
      "sample_monotonic_ns": 86400000000000,
      "counters": {
        "reads_completed": 12450,
        "reads_merged": 320,
        "sectors_read": 840192,
        "read_ms": 15230,
        "writes_completed": 8100,
        "writes_merged": 450,
        "sectors_written": 524288,
        "write_ms": 12300,
        "io_in_progress": 0,
        "io_ms": 18500,
        "weighted_io_ms": 27530,
        "discards_completed": 0,
        "discards_merged": 0,
        "sectors_discarded": 0,
        "discard_ms": 0,
        "flushes_completed": 0,
        "flush_ms": 0
      }
    }
  }
}
```

Only current, verified kernel samples appear in the raw file. Whole-device
statistics use the 11 base fields, 15 with discard statistics, or 17 with
flush statistics. Unsupported extensions are omitted and unavailable
`diskseq` is null. `io_in_progress` is an instantaneous gauge, so it can
fall during normal operation.

Each I/O metric uses a cumulative delta and actual monotonic elapsed time:

| Metric | Calculation |
|---|---|
| `busy_percent` | 100 × delta of I/O milliseconds ÷ elapsed milliseconds |
| `read_time_percent` | 100 × delta of read milliseconds ÷ elapsed milliseconds |
| `write_time_percent` | 100 × delta of write milliseconds ÷ elapsed milliseconds |
| `read_bytes_per_second` | 512 × delta of sectors read ÷ elapsed seconds |
| `write_bytes_per_second` | 512 × delta of sectors written ÷ elapsed seconds |
| `read_iops` | Delta of completed reads ÷ elapsed seconds |
| `write_iops` | Delta of completed writes ÷ elapsed seconds |

The kernel's sector unit is 512 bytes independent of device sector size.
Results are rounded to three decimal places. Overall busy time estimates
wall time with block I/O outstanding and is limited by kernel timestamp
granularity and accounting under concurrency. Directional times accumulate
across requests and can exceed 100%. They can overlap and do not sum to
overall utilization. None of these measurements determines spindle
position, thermal load or NAS idle eligibility.

Counter samples live in process memory. The first cycle after startup always
reports null for all seven metrics. Missing or ambiguous identity, changed
device generation, failed counter reads and nonpositive elapsed time
invalidate the comparison. A decrease in any cumulative counter present in
both samples also invalidates all seven metrics, including decreases in
merge, weighted-I/O, discard or flush counters. The instantaneous
`io_in_progress` gauge is excluded. A current valid sample is still
published in the raw snapshot and forms the next baseline. The service
never reads the raw snapshot back or changes the kernel counters.

References: [Linux block stat field definitions](https://docs.kernel.org/block/stat.html)
and [kernel I/O accounting](https://docs.kernel.org/admin-guide/iostats.html).

## Observed I/O idle duration

`io.idle_seconds` measures the contiguous duration for which consecutive
kernel samples show no block I/O activity. It is a nonnegative integer
rounded down from actual elapsed monotonic time. The monitor retains
fractional seconds in memory. Each configured disk has its own history.

Any increase in a cumulative counter, including discard, flush, merge
and weighted-I/O statistics when available, is evidence of activity. A
nonzero `io_in_progress` at either end of the interval also indicates
activity, even when the gauge was higher at the preceding sample. On
activity, idle duration resets to zero at the current sample. Credit
starts at that sample only if a later comparison establishes an idle
interval. This avoids estimating when an I/O within the interval ended.

The first sample reports null. A restart, missing or ambiguous disk,
changed ATA serial or device generation, failed counter read, decreased cumulative
counter, changed set of available fields, nonpositive elapsed time, or
sample gap longer than twice the configured interval breaks continuity.
Idle duration is null at the break. The next valid sample, including the
current sample when it is valid, becomes the new baseline. Idle history
is never loaded from a previous snapshot. A changed field set or long
gap can leave the seven rate and utilization fields available while idle
duration is null.

Idle duration is limited to activity visible in the available Linux
block statistics and to identity changes the host can observe. A drive may stay
spinning, carry out firmware work, or run a SMART self-test throughout
an observed idle interval. Standby, unknown spindle state, unavailable
temperature and SMART read failures do not interrupt valid kernel
observations. An identity verification failure still invalidates the
sample. Use idle duration as passive evidence. Standby authorization
requires a separate policy and current checks.

## Post-hooks

After both files publish successfully, each idle hook receives the exact
processed JSON bytes through stdin, including snapshots with missing or
sleeping drives. An argument array executes directly. A busy hook skips
the current snapshot; delivery has no retry queue. Hooks execute
independently with bounded timeouts. Exit errors and timeouts appear in
the journal, and stdout and stderr are discarded. The service terminates
remaining processes in the hook's process group on timeout or shutdown.
Hooks are intended as foreground consumers. Under systemd the unit's cgroup
also covers teardown of descendants.

A generic consumer can inspect the entire snapshot from stdin. This example
uses the process exit status to indicate that it successfully parsed the
message and found the expected schema.

```python
#!/usr/bin/env python3
import json
import sys

snapshot = json.load(sys.stdin)
raise SystemExit(0 if snapshot.get("schema_version") == 4 else 1)
```
