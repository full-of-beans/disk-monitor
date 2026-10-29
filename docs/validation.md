# Validation and CENMATE commissioning plan

## Software evidence

Run Ruff formatting/lint, `python3 -m unittest discover -s tests -v`, installed
CLI validation and the CI Python matrix. Fixtures cover parsing, exact identity
and aliases, duplicate/ambiguous/replaced identity, command errors/timeouts,
independent deadlines, I/O resets and gaps, reconnect generations, retained
measurement times, atomic publication, installed layout, lifetime lock, real
subprocess timeout, and foreground SIGTERM/restart. These are software tests,
not evidence of enclosure behavior. No actual HDD command is needed for testing.

## Hardware window (requires separate operator authorization)

Before deployment, use the actual six-bay CENMATE, the final host kernel,
smartmontools/hdparm versions, and each actual drive/bridge firmware combination.
Record source commit, software versions and a serial/bay mapping. Store a concise
sanitized outcome in this repository; keep raw system exports and secrets out.

1. **Identity without wakeups:** with existing known standby drives, run metadata
   discovery only and compare each serial/alias with established physical
   inventory. Cross-check all six bays and shared bridge serials. Record exactly
   which cached field identifies each drive. No routine `smartctl -i` workaround.
2. **Single protected probes:** run the exact service command variants, one drive
   at a time. Use independent enclosure power/current observation and audible/
   tactile confirmation where practical to detect spin-up, including drives for
   which hdparm reports literal `unknown`. Confirm no spin-up for `hdparm -C`,
   protected SCT status and protected capabilities. Capture return codes and
   sanitized JSON; verify standby skip 3 and unsupported check 5 handling. If any
   probe wakes a drive, stop the monitor and classify that combination unsupported
   before enabling a background service.
3. **Active drives:** during an already authorized workload, compare temperature
   with an already authorized comprehensive SMART capture. Verify capabilities
   status on an already-running SMART test and again after completion. This
   monitor must neither start nor abort the test. Do not generate extra tests or
   waking work just to satisfy the checklist without approval.
4. **I/O comparison:** during the same workload, compare actual `stat` deltas,
   elapsed time and reported window/busy percentage; verify busy/idle transitions
   publish but repeated same-classification I/O samples do not; cross-check
   `iostat -x` if available. Internal SMART work need not produce host I/O utilization.
5. **Supervised foreground run:** use a separate tmpfs output; verify all four
   cadences over at least 15 minutes and sleeping media remain asleep. Arrange
   the NAS maintenance/lifecycle exclusion strategy first; do not concurrently
   duplicate periodic probes without review. Inspect process count, CPU, memory,
   USB logs and pending-command behavior.
6. **Recovery:** only with unmounted/test media or an operator-approved safe
   enclosure-off transition, disconnect/reconnect and check unknown/missing,
   new diskseq, discarded in-flight samples, clean baselines, reordered paths,
   preservation of last_active_at for the same serial within the process,
   detection within the 60-second discovery interval, and no history or
   temperature transfer to a replacement serial. Never unplug mounted disks
   for this test. If a safe replacement/duplicate-ID fixture is unavailable,
   record that physical case untested; keep the software tests as separate evidence.
7. **Lifecycle/publication:** terminate/restart foreground, then test the systemd
   unit in the approved window. Verify clean bounded exit, individual measurement
   expiry after stop without heartbeat writes, atomic readable JSON, cleared
   evidence (including last_active_at) on restart, and a rejected second
   writer. Check `systemd-analyze verify` on the installed target unit and audit
   file ownership/modes, output tmpfs, service permissions and restart logs.

Acceptance: exact six-drive mapping, no observed wakes or power commands,
correct active/inactive/unknown transitions, independent freshness and recovery,
no unintended NAS policy changes. Any failing/unsupported bridge, unverified
physical serial mapping, required NAS exclusion decision, or incomplete safe
recovery test blocks production deployment. Keep existing NAS implementation
available for rollback; this initial PR neither installs nor deploys to the NAS.
