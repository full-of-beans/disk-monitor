# Future NAS integration (not implemented)

`disk-monitor` owns passive observations and its own versioned JSON interface.
`nas-config` can install this exact service from a pinned reviewed commit, render
`/etc/disk-monitor/config.toml` from its master TOML, and restart it through its
normal configuration/deployment transaction. No NAS-specific fork or second
monitor service is needed. Runtime Python imports stay independent of NAS and
fan-controller repositories.

NAS retains idle accounting, storage-operation/lifecycle locks, standby
eligibility and authorization, fresh pre-effect verification, enclosure power,
maintenance scheduling and SMART request accounting. Busy percentage is not idle
accounting. NAS must continue reading trusted raw counters for its IdleTracker,
or deliberately extend this interface with equivalent counters/generation/time
contracts and corresponding tests. Cached self-test activity is diagnostic; NAS
must still perform its immediate firmware veto before a standby effect.

## Existing compatibility boundary

At NAS reference `8e8b5a1c3eb1ebb0fb931e6a956a729ceee41c59`,
`spindle_telemetry.py` publishes `/run/nas-disk-monitoring.json`, schema 1,
with top-level integer `observed_at` and `disks` keyed by serial. Rows include:

- `role`, `serial`, `presence`, `device`;
- `power_state` active/standby/unknown (null for SSD) and `power` raw diagnostic;
- NAS-owned `idle_seconds`, per-row integer `observed_at`;
- `last_observed_temperature_c` and independent integer
  `last_observed_temperature_at`.

`nas-hdd-status` reads cached idle and temperature; `nas_storage.observability`
reads identity, presence/power/idle and temperature for API/Cockpit projections.
The publisher also accepts SSD temperature captures and immediate verified
standby transitions from NAS operations. This service's schema-2 document is
**not** compatible. Do not simply point its output at the old filename or run
both publishers on that file.

Before replacement, the NAS integration PR must either migrate all consumers
atomically to the native schema or implement a NAS-owned projection with:

1. Exact serial-to-role mapping from authoritative inventory; bay is only a
   configured numeric physical slot. Preserve SSD observations through their
   NAS-owned path because this release observes ATA HDDs only.
2. Independent success times for presence, power and temperature. Floor positive
   epoch seconds to integer only for old consumers. Never use publication time
   as measurement time. Unknown/stale power stays unknown; retained temperature
   preserves its actual original time and is never refreshed by a spindle poll.
3. Explicit per-kind expiry and separate service-health checks, future-clock
   rejection, no cached standby evidence used to authorize enclosure power removal.
4. NAS-owned idle_seconds or null, with original continuous-counter and gap/reset
   protections. A rounded 0% utilization cannot supply this field.
5. A single writer for any legacy projection. Coordinate NAS verified-standby and
   comprehensive SMART temperature publishers, preserving ordering and identity;
   do not let an earlier monitor sample overwrite a later NAS verification.
6. Fixtures for API, Cockpit, status, idle authorization, SSD rows, stale/missing/
   ambiguous devices, replug/generation change, clock changes and rollback.
   Inventory all additional current/installed consumers at integration time.

## Migration sequence

1. Commission disk-monitor in a separately approved supervised window, using a
   separate output and avoiding duplicate periodic probes wherever possible.
2. Implement NAS rendering/install/verification with a pinned source version;
   stage configuration, validate it with the installed CLI, and test transaction
   failure and rollback using existing NAS conventions.
3. Review probe exclusion during NAS maintenance/lifecycle transitions. Current
   NAS temperature sampling acquires storage-operation and shared lifecycle
   locks; standalone monitoring intentionally knows none of these. Prove passive
   probes may coexist, or let NAS lifecycle orchestration stop/start this service
   around excluded transitions. Do not silently remove existing exclusion rules
   or teach this monitor NAS locks.
4. Split policy from the existing `nas-disk-monitor`: keep NAS IdleTracker and
   effect authorization; remove only duplicated passive observations once their
   consumers use the tested replacement. Do not disable the whole NAS monitor
   while it still owns idle standby.
5. Retire duplicated `nas-hdd-temperature.service`/timer and passive activity
   polling only after all replacements, state writers and consumers are coherent.
   Do not retire SMART accounting/health collection merely because current-test
   activity is available here.
6. In a separately authorized deployment, switch service/config/consumers as one
   reviewed change. Validate and retain rollback to previous units/config and
   source commit. No deployed state is claimed by this initial implementation.
