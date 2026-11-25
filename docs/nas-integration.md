# NAS integration

A separately reviewed NAS integration can install Disk Monitor at a pinned
commit and render its configuration from the authoritative NAS inventory.
Disk Monitor is the intended owner of periodic HDD I/O idle tracking.
The NAS owns serial-to-role and slot mapping, storage-operation and
lifecycle locks, maintenance scheduling, standby policy and authorization,
and spindle and enclosure power control.

Use schema 4 for passive consumers able to handle explicit missing or
unknown observations and age the entire snapshot. Direct fan-controller
communication belongs in that project's host integration. A generic
post-hook can deliver telemetry to an approved separate consumer.

## Migration and authorization

- The existing NAS telemetry contract described at NAS reference
  `8e8b5a1c3eb1ebb0fb931e6a956a729ceee41c59` uses
  `/run/nas-disk-monitoring.json` schema 1. Its consumers include
  `nas-hdd-status` and `nas_storage.observability`. Inventory all current
  readers and writers on the integration branch before switching. Keep
  Disk Monitor's outputs at separate paths during commissioning.
- NAS readers can migrate to schema 4 or use a NAS-owned projection.
  Keep SSD telemetry and NAS-owned policy fields on their existing sources.
  The snapshot completion time represents the whole cycle. A projection
  must preserve sampling uncertainty and the original observation age.
- Plan to replace periodic NAS HDD idle observations with Disk Monitor's
  `io.idle_seconds` after validating both during commissioning. Review
  differences in availability, continuity and reporting cadence before
  retiring the periodic HDD IdleTracker path. Treat null or stale idle
  observations as unavailable evidence for standby decisions.
- Keep immediate pre-actuation verification of current device identity,
  I/O and firmware self-test status in nas-config under its existing locks.
  Idle duration and other cached telemetry are inputs to policy. Standby
  and enclosure power removal still require authorization in nas-config.
- Review exclusion of monitoring during storage operations and enclosure
  transitions. Standalone deployment has no NAS lock coupling. The NAS may
  orchestrate stopping and starting the service across protected phases.
  Prove compatibility or implement exclusion before retiring the old polls.
- Commission the actual HDDs and USB bridges on separate paths before
  switching consumers. Retain the existing NAS policy services and a
  rollback to the previous config, consumers and unit versions. Retire
  duplicated passive observations only once their readers and ordering
  contracts are covered by tests.
