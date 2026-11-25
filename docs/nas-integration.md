# NAS integration

A separately reviewed NAS integration can install Disk Monitor at a pinned
commit and render its configuration from the authoritative NAS inventory.
Disk Monitor publishes passive evidence. The NAS owns serial-to-role and
slot mapping, idle accounting, storage-operation and lifecycle locks,
maintenance scheduling, standby authorization and enclosure power control.

Use schema 3 for passive consumers able to handle explicit missing or
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
- NAS readers can migrate to schema 3 or use an explicitly NAS-owned
  projection. Preserve SSD telemetry and NAS-owned idle/power fields using
  their existing sources. The snapshot completion time is not an exact
  per-observation timestamp. A compatibility projection must preserve that
  uncertainty and cannot refresh older samples using a new completion time.
- Continue authoritative idle accounting through the NAS IdleTracker or a
  deliberately reviewed equivalent. Three-decimal utilization and a
  monitoring gap are insufficient evidence for idle authorization. Keep
  immediate pre-action verification of device identity, I/O and firmware
  self-test status under the NAS's existing locks. A cached standby or
  inactive state never authorizes standby or enclosure power removal.
- Review exclusion of monitoring during storage operations and enclosure
  transitions. Standalone deployment has no NAS lock coupling. The NAS may
  orchestrate stopping and starting the service across protected phases.
  Prove compatibility or implement exclusion before retiring the old polls.
- Commission the actual HDDs and USB bridges on separate paths before
  switching consumers. Retain the existing NAS policy services and a
  rollback to the previous config, consumers and unit versions. Retire
  duplicated passive observations only once their readers and ordering
  contracts are covered by tests.
