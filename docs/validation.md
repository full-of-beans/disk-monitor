# Hardware commissioning

Hardware commissioning is outstanding. Software fixtures and CI validate
parsing, lifecycle, output and failure behavior. They cannot establish
whether a particular physical HDD and USB bridge keep a disk asleep.
Commission every drive/bridge model and firmware combination in a
supervised window before leaving polling enabled.

## Preparation

Use a temporary root-owned tmpfs output and enroll one drive at a time
initially. Choose unmounted test media or a separately approved maintenance
window. Coordinate with any NAS timers, filesystem access, and SMART daemons
so they cannot wake the drive during the experiment. Keep data backed up and
respect storage-operation and lifecycle locks. Use an independently observed
spindle signature, such as vibration at the test HDD or a separately
measured current trace. Enclosure LEDs and quiet kernel counters alone are
insufficient to prove the spindle stayed down.

| Check | Expected result and failure conditions |
|---|---|
| Native SATA identification | `disk-monitor discover` shows its exact physical serial, the correct node and `ata`. Verify against the drive label or a trusted prior inventory. A mismatched or duplicate serial blocks enrollment. |
| CENMATE identification | With the intended disks and bridges installed, map the physical serials to metadata and verify each resolves uniquely with `sat`. Confirm the serial is from the HDD, not the enclosure. An unknown transport or reused identity blocks SMART commissioning for that drive. |
| Awake command | While a separately authorized workload already keeps the drive awake, observe one combined check using the resolved device path and transport. Verify the JSON and diagnostics through `smartctl -d sat -n standby,3,5 -i -c -l scttempsts --json=o /dev/sdX` for USB, or substitute `-d ata` for native. Use only the independently mapped node. Active and explicit ATA idle modes permit the SMART reads and should publish `spindle: active`. If the drive reports Idle_C, record any transition from reduced spindle speed during the check. |
| Temperature | Compare a current SCT reading while awake with a trusted temperature captured during the same authorized workload. A drive missing SCT can still be enrolled for supported observations. Investigate unexplained disagreement or temperature on a confirmed sleeping drive. |
| Self-test activity | Observe a test already running under authorized maintenance and observe again after it finishes. Expect active during the test and inactive after a recognized completion. Missing evidence leaves the field unknown. This procedure neither starts nor stops a test. |
| Standby and sleep | Let the drive reach standby through its existing supported policy. On the next protected check, expect a standby diagnostic and exit 3, with null temperature and unknown self-test in telemetry. If hardware in an independently confirmed sleep state is available, verify that polling leaves it asleep and publishes unknown spindle and self-test with null temperature. The sleep diagnostic can also indicate a failed power check. Independently verify no spin-up, including any transient during the check. Wake signature, implausible active state, or an incorrect identity is a failure. Stop polling that drive and investigate. |
| Unsupported power check | If the bridge does not implement the check or reports an unrecognized mode, expect the smartctl stop diagnostic, generally exit 5, and unknown SMART observations. Independently verify that the power check itself did not wake the media. Do not try an unprotected command. Leave an unsafe or unsupported combination out of unattended SMART polling. |
| Repeated standby | Observe at least several scheduled cycles and a representative idle window on native and USB configurations. Verify absent wake signatures, no stale temperature and intact current snapshots. Compare baseline draw before and during monitoring. Then repeat with all six CENMATE slots populated. |
| Refresh after standby | With the monitor running, use a separately authorized external standby transition on the test drive. Independently confirm standby, then request a refresh with `systemctl kill --kill-whom=main --signal=SIGUSR1 disk-monitor.service`. Observe new processed and raw snapshots and verify that the intended drive reports `standby` with null temperature and unknown self-test. Check the spindle trace for a wake-up or transient spin-up throughout the refresh. Repeat on native SATA and on the intended CENMATE USB-SATA topology. |
| USB disconnect/reconnect | On isolated unmounted test media or with the NAS-authorized safe removal procedure, disconnect and observe at least one complete cycle. Expect the serial to remain in state as missing and disappear from raw counters. Reattach and verify exact serial, new generation, updated path if reassigned, and null rates and idle duration on the first valid sample. |
| Enclosure power cycle | Use the same safe removal procedure and approved power lifecycle. Verify identity and the new generation, confirm that the second valid counter sample resumes rates, and check that idle duration starts fresh. A reconnect with no distinguishable generation needs additional hardware review. |
| Service restart | Leave a populated raw file and processed snapshot in the output directory, then stop and start a foreground service using that directory. The first new state must show null rates and null idle duration per disk. The next valid comparison starts fresh. A managed systemd runtime directory may be removed on service stop. |
| Workload comparison | On authorized scratch media use sustained reads, writes, then mixed I/O. Compare stat deltas and elapsed time with `iostat -dx` over a matching window where possible. Check 512-byte sector conversion, expected directions and IOPS. Confirm `counters.json` includes all fields the kernel exposes, including discard and flush statistics. Directional times may exceed 100%, and mismatched windows or kernel accounting can cause differences. |
| Observed I/O idle duration | Compare `io.idle_seconds` with raw counter samples on quiet media. Verify growth from actual sample time across unchanged counters with no outstanding I/O, and a reset to zero when the workload registers activity. Verify that idle duration can grow in confirmed standby across repeated no-wake polls. If sampling is interrupted beyond twice the configured interval, expect null at the next observation. |
| Hooks and runtime | Use a sink on tmpfs that records stdin, plus a deliberately failing and a slow hook. Verify schema 4 and that the payload matches its published snapshot, including idle duration for missing and sleeping disks. Observe skipped overlapping calls, continued output, continued unrelated hook delivery, shutdown cleanup and the rejected second writer. |
| Installed service | Check output and config permissions, the writable runtime paths, `systemd-analyze verify`, and the journal from a supervised start and stop. Confirm rate gaps and outages are visible to the consuming process. |

Record the drive firmware, bridge/USB IDs, smartctl version, observed power
traces, actual command outcomes and unresolved failures. A software pass
permits code review. Unattended use requires a successful no-wake test,
verified identity, correct recovery and any NAS lifecycle exclusions on the
actual topology.
