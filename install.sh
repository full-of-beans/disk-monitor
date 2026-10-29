#!/bin/sh
# Install files only. Never enable/start/restart services or overwrite config.
set -eu
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=${DESTDIR:-}
install -d -m 0755 "$root/usr/local/lib/disk-monitor/disk_monitor" \
    "$root/usr/local/sbin" "$root/etc/disk-monitor" "$root/etc/systemd/system"
for file in "$source_dir"/disk_monitor/*.py; do
    install -m 0644 "$file" "$root/usr/local/lib/disk-monitor/disk_monitor/"
done
install -m 0755 "$source_dir/disk-monitor" "$root/usr/local/lib/disk-monitor/disk-monitor"
ln -sfn ../lib/disk-monitor/disk-monitor "$root/usr/local/sbin/disk-monitor"
install -m 0644 "$source_dir/systemd/disk-monitor.service" "$root/etc/systemd/system/"
install -m 0644 "$source_dir/config/example.toml" "$root/etc/disk-monitor/example.toml"
printf '%s\n' 'Files installed. Configure /etc/disk-monitor/config.toml before starting the service.'
