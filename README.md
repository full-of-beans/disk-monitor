# disk-monitor

A small Linux service that observes configured HDDs without intentionally waking
or changing their power state. Python 3.11+ standard library only at runtime.
No database, network listener, NAS dependency, or fan-control policy.

**Initial release scope:** whole `/dev/sd*` ATA/SATA HDDs, including SAT-capable
USB bridges. NVMe, SAS, hardware RAID and bridges requiring proprietary SMART
transports are unsupported. Missing metadata, unsupported commands and failures
remain unknown; they never imply idle, standby, a completed test, or good health.
This PR has software validation only. CENMATE commissioning is still required.

| Observation | Default interval | Method |
| --- | --- | --- |
| I/O busy percentage | 10 s | Delta of kernel `stat` I/O milliseconds over monotonic elapsed time |
| Spindle | 30 s | `hdparm -C` (10 s timeout) |
| Temperature, °C | 60 s | SAT `smartctl -n standby,3,5 -l scttempsts --json` (3 s timeout) |
| Current SMART self-test activity | 60 s | SAT `smartctl -n standby,3,5 -c --json` (3 s timeout) |

These are independent deadlines. Hardware commands serialize per drive; a slow
SMART command cannot block I/O observation or another drive. No SMART identity
probe, test start/abort, SMART enable, standby request, or fallback waking query
is issued. Standby protection depends on the actual bridge/drive implementation;
see the [hardware validation procedure](docs/validation.md).

## Install

Debian example (requires an operator-approved installation window):

```sh
sudo apt-get install python3 smartmontools hdparm
# Review and check out the desired disk-monitor commit first.
sudo sh ./install.sh
sudo cp /etc/disk-monitor/example.toml /etc/disk-monitor/config.toml
sudoedit /etc/disk-monitor/config.toml
sudo disk-monitor --config /etc/disk-monitor/config.toml validate
sudo systemctl daemon-reload
sudo systemctl enable --now disk-monitor.service
```

`install.sh` installs files only. It never starts a service or replaces
`config.toml`. `DESTDIR=/temporary/root sh ./install.sh` stages the same layout
for inspection. Updates use the same installer followed by an explicit restart;
stop the service before replacing its Python files. Keep code, configuration,
and the state directory root-owned and unwritable by other users. The service
needs root for ATA ioctls and access to `/sys`, cached udev data, and disk nodes.
The default `/run/disk-monitor` location is disposable tmpfs, preventing telemetry
writes from creating monitored HDD activity. A custom output directory must also
be on tmpfs and needs a matching systemd `ReadWritePaths` override.

Use smartmontools **7.3+** for the explicit unsupported-power-mode exit status;
Linux must expose `/sys/block/sd*/diskseq` (Linux 5.15+). An older/unsupported
installation fails conservatively; it is not a supported deployment target.
The installer does not install Python development tools or Python packages.

## Configure and run

Start with [config/example.toml](config/example.toml). Configure each drive's
actual serial and optional unique bay label. Intervals accept finite numbers
from 1 to 86400 seconds; omitted intervals use the defaults above. Unknown keys,
duplicate identities/aliases, duplicate bay labels and malformed values fail
validation. Configuration is loaded once; restart after changing it.

```sh
# Source checkout or installed CLI; discovery reads cached metadata only.
./disk-monitor discover
./disk-monitor --config config/example.toml validate
sudo disk-monitor --config /etc/disk-monitor/config.toml run
# In another terminal; Ctrl-C / SIGTERM stops foreground execution.
disk-monitor --config /etc/disk-monitor/config.toml status
```

`discover` works without a configuration. It lists kernel serial, cached udev
serial variants and full by-id basenames as exact candidate identities; it does
not certify which candidate belongs to a physical disk. Match against known
physical serials. When a USB bridge exposes a different identifier, add only a
**manually verified exact** `identity_aliases` entry. Never accept a shared
bridge serial as a per-drive identity. Unresolved devices receive no commands;
multiple matches, or two configured drives claiming one device, are ambiguous.
An unexpected replacement serial stays missing rather than inheriting a bay.

`status` reads the JSON only, recalculates freshness and exits 1 for a missing,
malformed, unsupported or stale service document. A live service with unknown
observations still exits 0: inspect each observation, not only the exit code.
`validate` exits 0 for valid configuration and 1 on errors. Argument errors exit 2.
Do not run foreground monitoring alongside the systemd service. A lifetime
output-file lock rejects a second writer using the same path.

## State and troubleshooting

Default output: `/run/disk-monitor/state.json`, schema version 1. Atomic rename
ensures readers see one complete document. Files are mode 0644; serials and bay
labels are visible to local users. There is no persistent telemetry history.
Each observation has independent attempt/success timestamps, source, error,
maximum age and freshness. Cached last successful temperatures are retained
through a failed/standby probe, without being presented as a fresh measurement.
See the [schema and design](docs/design.md) for exact consumer rules.

- `journalctl -u disk-monitor.service` reports fatal configuration/publication
  failures; per-probe failures appear in JSON `error` fields.
- `missing` / `ambiguous`: compare `discover` identities with the configured
  serials/aliases and verify the mapping physically. Do not replace identity
  with `/dev/sdX`, or use `smartctl -i` as a periodic workaround.
- `unknown`: confirm sysfs/udev permissions and `diskseq` support. Failed
  inventory enumeration does not prove absence.
- `standby` temperature/self-test errors are expected for sleeping drives.
  `smart_command_failed` can mean unsupported power checks or SAT transport;
  there is intentionally no unprotected fallback.
- `power_unavailable` / `timeout`: check permissions, bridge support and USB
  transport logs. A successful literal `hdparm` state of `unknown` is classified
  as `active`, matching NAS semantics; failed or malformed output is `unknown`.
- `baseline_required` is expected on startup, reconnect, counter reset or a
  polling gap. A 0% busy observation never authorizes standby.
- Stale service heartbeat: check service state and output permissions. Consumers
  must expire data themselves even if the file still says `fresh: true`.

## Development

```sh
python3 -m pip install -r requirements-dev.txt
ruff format --check .
ruff check .
python3 -m unittest discover -s tests -v
```

CI runs those checks on Python 3.11 and 3.13, installs the Python package and
validates the example. Tests include an isolated installed layout, real process
timeout, foreground SIGTERM/restart, and injected hardware failures; no real disk
commands are executed by tests. [Integration planning](docs/nas-integration.md)
defines how NAS deployment can later own the configuration and install this exact
service while retaining its own storage policy.
