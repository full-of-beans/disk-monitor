# Observation design and state contract

## Authority and reference review

Implementation starts from disk-monitor main
`a1e055ac9e19a2bdf0ef1772cc5f20084e293324` (README/license/ignore bootstrap; no AGENTS.md or existing
PRs at inspection). NAS reference main was
`8e8b5a1c3eb1ebb0fb931e6a956a729ceee41c59`.

Reviewed NAS AGENTS.md, README/current-state, configuration, Ruff policy,
`disk_monitoring.py`, `physical.py`, `temperature.py`, `spindle_telemetry.py`,
`smart_session.py`, `smart_evidence.py`, command bounding, inventory,
`nas-disk-monitor`, `nas-hdd-temperature-sample`, monitor/temperature systemd
units, associated tests, `nas-hdd-status`, and observability consumers.

Reused behavioral contracts, not the NAS orchestration graph:

- One complete successful hdparm result line; positive literal `standby` is
  standby, any other parsed successful state is active. Failed/multiple/malformed
  lines remain unknown. Retain raw state for diagnostics.
- Explicit SAT type, standby exit 3 and unsupported power check exit 5, narrow
  SCT status query, signed Celsius validation excluding -128. No fallback probe.
- SMART lower three error bits invalidate observations; health/log warning bits
  alone do not invalidate a successfully decoded measurement. No health verdict
  is inferred from ignoring those higher bits.
- Current ATA status high nibble 15 means self-test active; 0..8 inactive;
  missing/reserved/invalid data means unknown. No historical-log inference.
- Serial identity and independent measurement times remain authoritative.

Unlike the NAS health session, the narrow activity query does not request `-i`,
`-a`, or historical logs. Identity comes from metadata, checked before the command
and again before accepting its result. No NAS lock, standby, maintenance ledger,
or lifecycle code is imported. These omissions are deliberate ownership boundaries.

## Identity

Whole `sd[a-z]+` devices only. Discovery collects `/sys/block/*/device/serial`,
udev database `ID_SERIAL_SHORT`, `ID_SCSI_SERIAL`, `ID_SERIAL`, and exact full
`/dev/disk/by-id` basenames pointing to a whole device. It never parses a guessed
serial out of a link name or scans SMART identity. Aliases require operator
verification because USB bridges may expose a bridge identity instead of a disk
identity. Shared/multiple identities fail closed. Unknown physical serials do
not replace a configured serial automatically.

The identity generation includes resolved sysfs location, major:minor and
`diskseq`. Missing diskseq makes a device ineligible. Any generation or identity
metadata change clears per-generation observations and I/O baselines, preserving
only drive-level `last_active_at` for the same configured serial. Command results
from an old generation are discarded. Hot unplug can race the final userspace
check and device open; metadata checks cannot eliminate that kernel/device race.
All commands remain read-only and standby-protected. Device identity spoofing or
stale/incorrect udev serial data is outside a passive monitor's trust boundary.

## Polling and command boundary

The lightweight one-second scheduler discovers at startup and reconciles every
`discovery_interval` seconds (default 60). It collects completed futures, observes
due I/O and queues the oldest due hardware observation per drive. Full cached
metadata discovery remains at hardware boundaries: before commands, after
commands, and once per batch of completed results before acceptance. These checks
can detect changes before periodic reconciliation. I/O reads use cheap kernel
generation checks before and after sampling; a mismatch invalidates the row and
requests discovery on the next tick. Discovery checks generation at both ends
of metadata collection to reject mixed-generation identities. Firmware-supplied
serials, when present in narrow SMART output, must match known device identities.
At most one hardware command per drive is pending, with one worker slot per
configured drive (maximum 32). Hardware tasks do not overlap on a drive. I/O and
other drives remain independent; no retry queue or catch-up burst exists.
Successful/failed result collection schedules that kind's next deadline.
Measurement timestamps are captured in the worker when the probe completes;
queueing or a delayed scheduler never refreshes old evidence.
Intervals are minimum delays, not hard real-time guarantees. One blocked kernel
ioctl can outlive a userspace timeout in uninterruptible kernel sleep; systemd
owns the whole control group and enforces shutdown as far as Linux permits.

Commands use argv arrays, no shell, no stdin, a fixed system PATH and C locale.
Only `hdparm -C` (10 s), and the two fixed protected `smartctl` queries (3 s)
exist. No runtime user-defined command/transport arguments. Unsupported bridges
stay unknown. SIGINT/SIGTERM stops scheduling, waits for bounded pending commands
and exits. Restart clears all runtime evidence, independently of old state files.

Busy percentage is `100 * delta(io_ticks_ms) / (1000 * elapsed_monotonic_s)`,
clamped to 0..100. Field 10 of kernel stat is used, not weighted queue time.
A baseline, nondecreasing counter, positive elapsed time and a gap no longer than
twice the configured interval are required. `window_seconds` records the actual
averaging window. This is approximate kernel I/O utilization; it does not measure
mechanical spindle motion, SMART internal work, idle duration or maximum device
throughput. Kernel accounting can undercount concurrent long requests.

## JSON v2

Top-level keys: `schema_version` (integer 2), `published_at` (UTC Unix seconds),
`disks` (object keyed by configured serial).
Each disk has `serial`, `bay` (unique positive integer/null), `presence` (present/missing/ambiguous/
unknown), `device` (current path/null), `device_generation` (array/null),
`presence_observed_at`, `last_active_at` (Unix seconds/null), and `observations` with `io`, `spindle`, `temperature`,
`self_test`. Time values are Unix seconds, potentially fractional; intervals
and averaging use a monotonic clock.

| Observation field | Meaning |
| --- | --- |
| `value` | Current successful value; null for unavailable numeric data, `unknown` for unavailable enums |
| `attempted_at` | Latest completed attempt, including failure; null before an attempt |
| `observed_at` | Latest attempt's successful measurement time, otherwise null |
| `last_value`, `last_success_at` | Last successful evidence in this device generation; may be stale |
| `error` | null on success; stable reason otherwise |
| `source` | `sysfs:stat:io_ticks`, `hdparm:-C`, `smartctl:scttempsts`, `smartctl:capabilities` |
| `max_age_seconds` | max(2 × configured interval, interval + 15 s) |
| `fresh` | Derived freshness at publication/read time; never a lasting assertion |

Spindle adds `raw_state`; I/O adds `window_seconds` and `activity`
(`busy`/`idle`/`unknown`). Busy means an increase in I/O milliseconds or any
trusted request/sector/time counter (kernel fields 1..8 and 12..15), even if
the short request added zero milliseconds. No hysteresis is used. Counter resets,
gaps and failed reads are unknown; an initial sample establishes only a baseline. Numeric observations use
percent and signed °C; spindle values are active/standby/unknown; self-test values
are active/inactive/unknown. Failed observations retain last successful evidence
but clear current validity. Presence/generation changes clear per-observation retained evidence.

Consumers MUST validate schema/types and recompute freshness using their current
clock: the particular successful observation age must be 0..max_age_seconds
with presence present. A future timestamp, missing/null timestamp or failed probe
is unknown. There is no `service_fresh` field or publication-age freshness test.
A valid old file cannot prove the service is alive. Use systemd/process health
separately; observations expire independently even if no more files are written. `published_at` and `attempted_at` never refresh a measurement. Cached
`last_value` must always be displayed with `last_success_at` and stale labeling.
`disk-monitor status` recomputes these booleans; raw file readers must do likewise.
These observations do not authorize power effects or assert overall disk health.

Publication uses exclusive random temporary files, mode 0644, flush + atomic
rename in the same directory, with cleanup on failure. No fsync is needed for
disposable tmpfs data. One lifetime flock per output prevents competing writers.
The protected parent directory is required; a malicious local writer in that
directory is outside the deployment trust model. Publication errors fail the
service so systemd can restart it.

Schema 2 deliberately removes the schema-1 heartbeat contract, changes bay labels
to numbers and adds drive activity history and I/O classification. Consumers must
coordinate this interface change and reject unsupported versions; no compatibility
shim is provided for the unmerged initial release.

## Publication, activity history and completion hooks

Authoritative rows, deadlines and I/O baselines live in memory. Publish at startup,
on presence/device/identity changes, on every accepted temperature/spindle/SMART
completion including failure, and on I/O activity classification changes.
Simultaneous changes are coalesced by one publication after a scheduler iteration.
An unchanged presence refresh, utilization percentage, I/O attempt timestamp, or
`last_active_at` update from another busy sample does not itself publish.
Snapshots triggered by other events include the latest in-memory I/O evidence.
Readers can see an older, eventually stale I/O sample despite a running monitor;
that is intentional and never grants freshness to an unpublished measurement.

`last_active_at` is the latest trustworthy spinning evidence for the configured
serial, not an inferred spin-down time or continuous activity interval. It updates
from a successful spindle result with raw `active/idle`, `active`, `idle`,
`idle_a`, `idle_b` or `idle_c`; an actual I/O counter increase over a valid window;
or current firmware self-test status reporting a running test. A successful
literal hdparm `unknown` retains the existing NAS-compatible `active`
classification but does **not** prove spinning for this history field. Standby,
failed/unknown probes, baseline/idle samples, cached temperature alone, and an
inactive historical test status do not advance it. The narrow temperature query
currently supplies no independent proof of spinning, so it does not advance this
field. Future validated temperature sources may explicitly supply that proof.
No hardware observation is synthesized into another observation's timestamp.
Older worker completions never move `last_active_at` backwards.

Standby, idle, missing/ambiguous/unknown presence, renumbering and a new kernel
generation preserve this timestamp for the same serial during this process.
All runtime evidence, including `last_active_at`, resets to null on restart;
old JSON is never restored. Changing configured serial starts a distinct row and
cannot inherit history from the previous drive or bay. Consumers must reject
future timestamps after wall-clock corrections rather than infer elapsed activity.

`Monitor.completions` is an internal standard-library bounded queue (32 events),
not an external protocol. After an accepted observation is recorded, it receives
`{serial, kind, completed_at, state}`. `state` is a detached full snapshot with all
configured drives, current temperatures, presence, bay and activity history.
A temperature completion (success or failure) therefore offers a complete
current temperature snapshot without waiting for a JSON write. I/O completions
also notify even when classification is unchanged. Invalid old-generation
results are discarded, never emitted as measurements for a new device.

A future adapter drains this queue in its own worker using `get()` or
`get_nowait()`, filters `kind == "temperature"` as needed, and performs transport
work outside the monitor. Multiple readers compete for events; this is one
mailbox, not a broadcast API. A full queue drops its oldest event and retains the
latest full snapshot, bounding memory and never waiting for a slow or absent
consumer. Delivery is best effort, with no acknowledgement/retry/history promise.
No Unix socket, USB serial, RP2350 protocol or bridge service is implemented.
The future transport must own connection/reconnection and consumer-side expiry.

The NAS reference was rechecked for this follow-up at the same main SHA above:
its publisher preserves temperature time independently of spindle publication;
its IdleTracker uses continuous trusted counters but owns NAS standby policy.
It has no equivalent persisted drive-level last-active field. Only those
observation/counter principles are reused here, with no NAS power policy imports.

## Temperature/spindle optimization decision

Disabled. Exit 3 proves the protected query skipped a low-power device under the
smartctl contract, but a successful temperature alone does not prove a canonical
hdparm state. The two queries have independent failure modes, timeouts and
freshness. This release intentionally never refreshes spindle evidence from a
temperature query. A later optimization must define exact supported evidence,
retain the raw source, test unsupported/error/racing paths, and obtain enclosure
hardware evidence. No fan curve or PI control strategy is selected here.

## Upstream references

- [Linux kernel I/O statistics](https://docs.kernel.org/admin-guide/iostats.html):
  field 10 accounting, resets, and approximation limitations.
- [smartmontools smartctl manual source](https://github.com/smartmontools/smartmontools/blob/master/smartmontools/smartctl.8.in):
  `-n` status/status2, explicit device type to avoid autodetection wakeups,
  capabilities and SCT status, exit status bitmask.

Reference behavior does not substitute for actual bridge commissioning.
