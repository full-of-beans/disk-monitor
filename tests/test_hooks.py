import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from disk_monitor.config import Config, Drive, Hook
from disk_monitor.discovery import Device, Generation
from disk_monitor.hooks import Hooks
from disk_monitor.probes import Smart
from disk_monitor.service import Monitor

RECEIVER = """
import json
import sys
from pathlib import Path
snapshot = sys.stdin.buffer.read()
assert snapshot == Path(sys.argv[1]).read_bytes()
Path(sys.argv[2]).write_text(json.dumps([json.loads(snapshot), sys.argv[3]]))
"""


class HookTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def hooks(self, *hooks):
        manager = Hooks(hooks)
        self.addCleanup(manager.close)
        return manager

    def test_real_hooks_receive_published_snapshot_and_literal_arguments(self):
        state = self.root / "state.json"
        paths = [self.root / name for name in ("received-1", "received-2")]
        literal = f"; touch {self.root / 'unexpected'}"
        config = Config(
            (Drive("S"), Drive("M")),
            output=state,
            counters=self.root / "counters.json",
            post_hooks=tuple(
                Hook(
                    f"receiver-{index}",
                    (sys.executable, "-c", RECEIVER, str(state), str(path), literal),
                )
                for index, path in enumerate(paths)
            ),
        )
        disk = Device(
            "sda",
            frozenset(("S",)),
            Generation("/sys/sda", "8:0", 12, 1),
            "sat",
            ata_serial="S",
        )

        def unavailable(_):
            raise OSError("test counters unavailable")

        monitor = Monitor(
            config,
            threading.Event(),
            discoverer=lambda: [disk],
            validator=lambda _: True,
            node_validator=lambda _: True,
            counter=unavailable,
            probe=lambda _: Smart("standby"),
        )
        self.addCleanup(monitor.close)
        monitor.cycle()
        for future in monitor.hooks.pending.values():
            future.result(timeout=3)
        for path in paths:
            document, argument = json.loads(path.read_text())
            self.assertEqual(document, json.loads(state.read_text()))
            self.assertEqual(document["disks"]["M"]["presence"], "missing")
            self.assertEqual(document["disks"]["S"]["spindle"], "standby")
            self.assertEqual(argument, literal)
        self.assertFalse((self.root / "unexpected").exists())

    def test_failures_and_timeout(self):
        manager = self.hooks(
            Hook("nonzero", (sys.executable, "-c", "raise SystemExit(23)")),
            Hook("missing", (str(self.root / "missing"),)),
            Hook("timeout", (sys.executable, "-c", "import time; time.sleep(20)"), 0.1),
        )
        with self.assertLogs("disk_monitor.hooks", level="WARNING") as logs:
            manager.dispatch(b"{}")
            for future in manager.pending.values():
                future.result(timeout=3)
        for message in ("status 23", "missing could not start", "timeout timed out"):
            self.assertTrue(any(message in log for log in logs.output), logs.output)

    def test_skip_while_running_without_blocking_other_hook_or_cycle(self):
        gate = self.root / "gate"
        slow = self.root / "slow"
        quick = self.root / "quick"
        manager = self.hooks(
            Hook(
                "slow",
                (
                    sys.executable,
                    "-c",
                    """
import sys, time
from pathlib import Path
with Path(sys.argv[1]).open('ab') as stream:
    stream.write(sys.stdin.buffer.read())
while not Path(sys.argv[2]).exists():
    time.sleep(0.01)
""",
                    str(slow),
                    str(gate),
                ),
            ),
            Hook(
                "quick",
                (
                    sys.executable,
                    "-c",
                    """
import sys
from pathlib import Path
with Path(sys.argv[1]).open('ab') as stream:
    stream.write(sys.stdin.buffer.read())
""",
                    str(quick),
                ),
            ),
        )
        manager.dispatch(b"first\n")
        manager.pending["quick"].result(timeout=3)
        first = manager.pending["slow"]
        with self.assertLogs("disk_monitor.hooks", level="WARNING") as logs:
            manager.dispatch(b"second\n")
            manager.pending["quick"].result(timeout=3)
        self.assertIn("still running", logs.output[0])
        self.assertIs(manager.pending["slow"], first)
        self.assertFalse(first.done())
        self.assertEqual(quick.read_bytes(), b"first\nsecond\n")
        gate.touch()
        first.result(timeout=3)
        self.assertEqual(slow.read_bytes(), b"first\n")

    def test_shutdown_terminates_running_process_group(self):
        marker = self.root / "pids"
        manager = self.hooks(
            Hook(
                "group",
                (
                    sys.executable,
                    "-c",
                    """
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}')
time.sleep(60)
""",
                    str(marker),
                ),
                timeout=60,
            )
        )
        manager.dispatch(b"{}")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if marker.exists() and len(marker.read_text().split()) == 2:
                break
            time.sleep(0.01)
        parent, child = map(int, marker.read_text().split())
        manager.close()
        manager.pending["group"].result(timeout=1)
        with self.assertRaises(ProcessLookupError):
            os.kill(parent, 0)
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{child}/stat").read_text().split(") ", 1)[1][0]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            self.fail("hook child still running after shutdown")
