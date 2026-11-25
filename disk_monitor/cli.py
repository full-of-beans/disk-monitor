"""Operator CLI; status/discovery/validation never run hardware probes."""

import argparse
import json
import logging
import signal
import sys
from contextlib import ExitStack
from dataclasses import asdict

from disk_monitor.config import load
from disk_monitor.discovery import discover
from disk_monitor.service import CycleRequests, serve


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
                document = json.load(stream)
            print(json.dumps(document, indent=2))
            return 0
        else:
            logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
            with ExitStack() as stack:
                requests = CycleRequests()
                stack.callback(requests.close)
                for signum, handler in (
                    (signal.SIGUSR1, lambda *_: None),
                    (signal.SIGINT, requests.request_stop),
                    (signal.SIGTERM, requests.request_stop),
                ):
                    previous = signal.signal(signum, handler)
                    stack.callback(signal.signal, signum, previous)
                # The low-level handler wakes poll even if a worker receives the signal.
                # A full pipe already marks work pending. The stop handler also sets a flag.
                previous_fd = signal.set_wakeup_fd(
                    requests.signal_fd, warn_on_full_buffer=False
                )
                stack.callback(signal.set_wakeup_fd, previous_fd)
                serve(config, requests)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"disk-monitor: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
