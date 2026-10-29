"""Operator CLI; status/discovery/validation never run hardware probes."""

import argparse
import json
import signal
import sys
import threading
from dataclasses import asdict

from disk_monitor.config import load
from disk_monitor.discovery import discover
from disk_monitor.service import serve
from disk_monitor.state import with_freshness


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/disk-monitor/config.toml")
    parser.add_argument("command", choices=("validate", "discover", "status", "run"))
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            devices = discover()
            print(
                json.dumps(
                    [asdict(d) | {"identities": sorted(d.identities)} for d in devices],
                    indent=2,
                )
            )
            return 0
        config = load(args.config)
        if args.command == "validate":
            print("Configuration valid")
        elif args.command == "status":
            with config.output.open() as stream:
                document = with_freshness(json.load(stream))
            print(json.dumps(document, indent=2))
            return 0
        else:
            stop = threading.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stop.set())
            serve(config, stop)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"disk-monitor: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
