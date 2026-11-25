# disk-monitor

Disk Monitor is a passive Linux service for explicitly enrolled rotational
ATA/SATA HDDs. It resolves devices by serial, reads Linux block I/O counters,
and uses one power-gated SMART command per eligible drive per cycle. The cycle
runs immediately and repeats every 60 seconds by default.

- `/run/disk-monitor/state.json` contains current-cycle spindle state,
  temperature, self-test activity, seven I/O metrics and observed I/O idle
  duration keyed by serial. The processed snapshot uses schema 4.
- `/run/disk-monitor/counters.json` contains raw Linux counters, device
  generation and actual sample times for diagnosis.
- Optional post-hooks receive the published state JSON on stdin. Slow and
  failed hooks are isolated from monitoring and each other.

Supported hardware is native libata HDDs and ATA HDDs on USB-SATA bridges with
safe SAT passthrough and cached udev ATA identification. NVMe, SSDs, native SAS,
RAID volumes and proprietary bridge transports are outside the hardware scope.
[Commission each drive and bridge](docs/validation.md) before unattended use.
The SMART power check permits polling in ATA idle modes, including Idle_C,
where polling can raise the spindle from reduced speed. The query is gated
at standby and sleep.
Disk Monitor never requests standby or starts or stops SMART tests.

`io.idle_seconds` counts whole seconds of observed block I/O inactivity.
It starts at null, grows across continuous observations with no I/O
activity, and resets to zero when the kernel statistics show activity.
Idle duration is independent of spindle state and provides passive evidence
for consumers that make their own policy decisions. See the
[idle duration contract](docs/design.md#observed-io-idle-duration).

## Install

Use Python 3.11+ and smartmontools 7.3+. On Debian-family systems with those
versions available:

```sh
sudo apt-get install python3 smartmontools
sudo sh ./install.sh
sudo cp /etc/disk-monitor/example.toml /etc/disk-monitor/config.toml
sudoedit /etc/disk-monitor/config.toml
sudo disk-monitor --config /etc/disk-monitor/config.toml validate
sudo systemctl daemon-reload
sudo systemctl enable --now disk-monitor.service
```

The installer copies program files, an example, and the unit. It preserves
`config.toml` and leaves service activation to the operator. Use
`DESTDIR=/temporary/root sh ./install.sh` for staging or
`python -m pip install .` for the CLI without the systemd or config files.
For an upgrade, stop the service before replacing its Python files, update
its configuration for the current schema, and then start it again.

The unit uses root for ATA passthrough, a managed runtime directory, a
read-only system filesystem, and access to real device nodes. Own program
files, configuration, temporary directories used for output, and hook
executables as root. Keep these paths unwritable by untrusted users. Hooks
run with the service's privileges. Use a tmpfs for outputs and hook-side
writes so polling does not generate I/O on the monitored storage. For custom
output paths under systemd, explicitly grant the parent directories as
writable via a unit override. Keep their lifetime and permissions under
operator control.

## Configure

```toml
interval = 60

[[drives]]
serial = "SERIAL_001"

[[drives]]
serial = "SERIAL_002"
# identity_aliases = ["ata-MODEL_SERIAL_002"]

# [output]
# path = "/run/disk-monitor/state.json"
# counters_path = "/run/disk-monitor/counters.json"

# [[post_hooks]]
# name = "example-consumer"
# command = ["/usr/local/bin/example-consumer"]
# timeout = 5
```

Only drive serials are required. Configure at least one exact ATA serial.
Use aliases only after confirming that the cached identity or complete by-id
basename denotes the intended physical HDD. A match shared by two devices
or two configured entries is ambiguous. Set the interval to 1–86,400 seconds.
Hook arguments are passed directly and the per-hook timeout defaults to 5
seconds (allowed 0.1–300).

```sh
disk-monitor discover
disk-monitor --config config/example.toml validate
disk-monitor --config /etc/disk-monitor/config.toml status
journalctl -u disk-monitor.service
```

`discover` reads cached Linux metadata only and reports candidates and their
transport. `status` prints the last published processed JSON. Use the unit
status, journal and top-level snapshot timestamp to diagnose stalled updates.
Missing disks, disabled SMART by a bridge, and individual observation failures
appear as missing or unknown values. See the [schema and design](docs/design.md)
for interpreting measurements and the [NAS integration notes](docs/nas-integration.md)
for policy ownership.

## Request a refresh

Once the running service has published its first snapshot, request an
additional monitoring cycle with:

```sh
systemctl kill --kill-whom=main --signal=SIGUSR1 disk-monitor.service
```

Use `--kill-who=main` on older systemctl versions that use that spelling.
The command sends a signal to the main process and returns asynchronously.
Refreshes use the same snapshots and hooks as regular cycles. Pending
requests coalesce, and an imminent regular cycle can satisfy them. Check
the published telemetry for the observation you need. If a post-cycle hook
requests refreshes, make its request conditional to prevent a feedback loop.
See the [scheduling rules](docs/design.md#on-demand-refresh).

## Troubleshoot

- If `discover` finds the expected serial with an unknown transport, check the
  device's sysfs ancestry and cached udev `ID_ATA` property. Adding an alias can
  resolve a verified identity, but cannot make a bridge eligible for SAT.
- If temperature stays null while a drive is awake, check `smartctl --version`
  and the service journal for the power-check or partial-data diagnostics.
  A drive can supply self-test status while omitting SCT temperature.
- If state stops advancing, check `systemctl status disk-monitor` and both
  output directories. A held writer lock, read-only custom output path, or
  snapshot write failure is reported in the journal.

## Development

```sh
python -m pip install -r requirements-dev.txt
ruff format --check .
ruff check .
python -m unittest discover -s tests -v
```
