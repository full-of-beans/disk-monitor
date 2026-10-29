"""Publication, identity and observation-completion regression contracts."""

import json
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from test_monitor import result

from disk_monitor import cli, config, discovery, probes, service, state


class Followup(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.disk = discovery.Device("sda", frozenset({"S"}), ("p", "8:0", "1"))
        self.visible = [self.disk]
        self.scan = Mock(side_effect=lambda: self.visible)
        self.count = (0, (0,))
        self.cfg = config.Config((config.Drive("S", 1),), dict(config.DEFAULTS))
        self.monitor = service.Monitor(
            self.cfg,
            discoverer=self.scan,
            counter=lambda _: self.count,
            validator=lambda d: d in self.visible,
            probe=lambda *_: ("active", None, {"raw_state": "active/idle"}),
            clock=lambda: self.now,
            epoch=lambda: self.now,
        )
        self.addCleanup(self.monitor.close)
        # Exercise scheduler/discovery/I/O without hardware completion events.
        self.monitor.due.update({("S", k): 10000 for k in config.DEFAULTS if k != "io"})

    def quiet_start(self):
        self.monitor.reconcile(self.monitor.inventory())
        self.monitor.next_discovery = self.now + 60
        self.monitor.due.update({("S", k): 10000 for k in config.DEFAULTS if k != "io"})
        self.monitor.tick()

    def sample(self, count=None):
        self.now += 10
        if count is not None:
            self.count = count
        return self.monitor.tick()["disks"]["S"]

    def test_periodic_discovery_and_reconnection(self):
        self.quiet_start()
        self.assertEqual(self.scan.call_count, 1)
        for _ in range(5):
            self.sample()
        self.assertEqual(self.scan.call_count, 1)
        self.visible = []
        self.sample()
        self.assertEqual(self.scan.call_count, 2)
        self.assertEqual(self.monitor.rows["S"]["presence"], "missing")
        self.visible = [discovery.Device("sdb", frozenset({"S"}), ("q", "8:16", "2"))]
        self.now += 60
        self.monitor.tick()
        self.assertEqual(self.monitor.rows["S"]["device"], "/dev/sdb")
        self.assertIsNone(self.monitor.rows["S"]["observations"]["io"]["value"])

    def test_no_heartbeat_and_io_only_publishes_classification_changes(self):
        self.quiet_start()
        with patch.object(service, "publish") as publish:
            self.monitor.publish_changes()
            self.assertEqual(publish.call_count, 1)
            self.sample()  # baseline -> idle
            self.monitor.publish_changes()
            self.assertEqual(publish.call_count, 2)
            for _ in range(3):
                self.sample()
                self.monitor.publish_changes()
            self.assertEqual(publish.call_count, 2)
            self.sample((100, (1,)))  # idle -> busy
            self.monitor.publish_changes()
            self.assertEqual(publish.call_count, 3)
            self.sample((200, (2,)))  # another busy measurement, no write
            self.monitor.publish_changes()
            self.assertEqual(publish.call_count, 3)
            self.monitor.record("S", "temperature", 42, None)
            self.monitor.record("S", "self_test", None, "timeout")
            self.monitor.publish_changes()  # simultaneous completions coalesce
            self.assertEqual(publish.call_count, 4)
            doc = publish.call_args.args[1]
            self.assertEqual(
                doc["disks"]["S"]["observations"]["io"]["observed_at"], self.now
            )
            self.sample()  # busy -> idle
            self.monitor.publish_changes()
            self.assertEqual(publish.call_count, 5)
            self.monitor.counter = Mock(side_effect=OSError())
            self.sample()  # idle -> unknown
            self.monitor.publish_changes()
            self.assertEqual(publish.call_count, 6)
            self.sample()  # another failed I/O sample, no classification change
            self.monitor.publish_changes()
            self.assertEqual(publish.call_count, 6)

    def test_last_active_only_trustworthy_activity_and_preserved_by_serial(self):
        self.quiet_start()
        row = self.monitor.rows["S"]
        self.assertIsNone(row["last_active_at"])
        # An actual short request can consume zero accounting milliseconds.
        self.sample((0, (1,)))
        self.assertEqual(row["last_active_at"], 110)
        self.sample()
        self.assertEqual(row["last_active_at"], 110)
        for kind, value, error, extra in [
            ("spindle", "standby", None, {"raw_state": "standby"}),
            ("spindle", "active", None, {"raw_state": "unknown"}),
            ("temperature", 42, None, {}),
            ("self_test", "inactive", None, {}),
            ("temperature", None, "timeout", {"confirms_active": True}),
        ]:
            self.monitor.record("S", kind, value, error, extra)
            self.assertEqual(row["last_active_at"], 110)
        self.monitor.record(
            "S", "spindle", "active", None, {"raw_state": "active/idle"}
        )
        self.assertEqual(row["last_active_at"], 120)
        self.now += 1
        self.monitor.record("S", "self_test", "active", None, {"confirms_active": True})
        self.assertEqual(row["last_active_at"], 121)
        # An older worker completion never moves a newer activity time backwards.
        self.monitor.record(
            "S", "spindle", "active", None, {"raw_state": "active/idle"}, stamp=115
        )
        self.assertEqual(row["last_active_at"], 121)
        self.monitor.reconcile({"S": ("missing", None)})
        self.monitor.reconcile(
            {
                "S": (
                    "present",
                    discovery.Device("sdb", frozenset({"S"}), ("q", "8:16", "2")),
                )
            }
        )
        self.assertEqual(row["last_active_at"], 121)
        restarted = service.Monitor(self.cfg)
        try:
            self.assertIsNone(restarted.rows["S"]["last_active_at"])
        finally:
            restarted.close()
        replacement = service.Monitor(
            config.Config((config.Drive("T", 1),), dict(config.DEFAULTS))
        )
        try:
            self.assertIsNone(replacement.rows["T"]["last_active_at"])
        finally:
            replacement.close()

    def test_completion_queue_complete_snapshot_is_bounded_and_isolated(self):
        self.quiet_start()
        while not self.monitor.completions.empty():
            self.monitor.completions.get_nowait()
        self.monitor.record("S", "temperature", 42, None)
        event = self.monitor.completions.get_nowait()
        self.assertEqual((event["kind"], event["serial"]), ("temperature", "S"))
        self.assertEqual(event["state"]["disks"]["S"]["bay"], 1)
        self.assertEqual(
            event["state"]["disks"]["S"]["observations"]["temperature"]["value"], 42
        )
        event["state"]["disks"]["S"]["serial"] = "tampered"
        self.assertEqual(self.monitor.rows["S"]["serial"], "S")
        for i in range(100):
            self.now += 1
            self.monitor.record("S", "temperature", None, "timeout")
        self.assertEqual(self.monitor.completions.qsize(), 32)
        events = [self.monitor.completions.get_nowait() for _ in range(32)]
        self.assertEqual(events[-1]["completed_at"], self.now)
        self.assertEqual(
            events[-1]["state"]["disks"]["S"]["observations"]["temperature"]["error"],
            "timeout",
        )

    def test_temperature_event_includes_other_drives_latest_state(self):
        cfg = config.Config(
            (config.Drive("S", 1), config.Drive("T", 2)), dict(config.DEFAULTS)
        )
        monitor = service.Monitor(cfg)
        try:
            monitor.record("S", "temperature", 42, None)
            monitor.record("T", "temperature", 43, None)
            monitor.completions.get_nowait()
            disks = monitor.completions.get_nowait()["state"]["disks"]
            self.assertEqual(disks["S"]["observations"]["temperature"]["value"], 42)
            self.assertEqual(disks["T"]["observations"]["temperature"]["value"], 43)
        finally:
            monitor.close()

    def test_completion_rechecks_live_identity_and_preserves_completion_time(self):
        self.quiet_start()
        future = Future()
        future.set_result((42, None, {}, 101))
        self.monitor.pending["S"] = (future, self.disk, "temperature")
        self.now = 110
        obs = self.monitor.tick()["disks"]["S"]["observations"]["temperature"]
        self.assertEqual(obs["observed_at"], 101)
        future = Future()
        future.set_result((99, None, {}, 111))
        self.monitor.pending["S"] = (future, self.disk, "temperature")
        self.visible = [discovery.Device("sda", frozenset({"T"}), ("p", "8:0", "2"))]
        row = self.monitor.tick()["disks"]["S"]
        self.assertEqual(row["presence"], "missing")
        self.assertIsNone(row["observations"]["temperature"]["last_value"])

    def test_worker_postcheck_and_firmware_identity_contradiction(self):
        def unplug(*_):
            self.visible = []
            return 42, None, {}

        self.monitor.probe = unplug
        self.assertEqual(
            self.monitor.hardware("S", self.disk, "temperature")[1], "identity_changed"
        )
        runner = Mock(
            return_value=result('{"temperature":{"current":42},"serial_number":"T"}')
        )
        self.assertEqual(
            probes.observe("temperature", self.disk, runner)[1], "identity_changed"
        )

    def test_duplicate_identity_at_collection_discards_completed_sample(self):
        self.quiet_start()
        future = Future()
        future.set_result((42, None, {}, 101))
        self.monitor.pending["S"] = (future, self.disk, "temperature")
        self.visible.append(
            discovery.Device("sdb", frozenset({"S"}), ("q", "8:16", "2"))
        )
        row = self.monitor.tick()["disks"]["S"]
        self.assertEqual(row["presence"], "ambiguous")
        self.assertIsNone(row["observations"]["temperature"]["last_value"])

    def test_counter_read_race_does_not_refresh_activity(self):
        self.quiet_start()
        self.sample()

        def replaced(_):
            self.visible = []
            return 500, (50,)

        self.monitor.counter = replaced
        row = self.sample()
        self.assertEqual(row["presence"], "unknown")
        self.assertIsNone(row["last_active_at"])

    def test_freshness_without_publication_or_heartbeat(self):
        self.quiet_start()
        self.monitor.record("S", "temperature", 42, None)
        self.monitor.record("S", "spindle", "standby", None)
        doc = self.monitor.snapshot()
        refreshed = state.with_freshness(doc, 170)
        self.assertTrue(refreshed["disks"]["S"]["observations"]["temperature"]["fresh"])
        self.assertFalse(refreshed["disks"]["S"]["observations"]["spindle"]["fresh"])
        self.assertFalse(
            state.with_freshness(doc, 221)["disks"]["S"]["observations"]["temperature"][
                "fresh"
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "state.json"
            state.publish(output, doc)
            with (
                patch.object(
                    cli,
                    "load",
                    return_value=config.Config(
                        self.cfg.drives, self.cfg.intervals, output
                    ),
                ),
                patch("builtins.print"),
            ):
                # Even an old valid snapshot is readable; CLI reports each expiry.
                self.assertEqual(cli.main(["status"]), 0)
            self.assertEqual(json.loads(output.read_text())["published_at"], 100)

    def test_bay_and_discovery_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            base = '[[drives]]\nserial="S"\n'
            for bad in ('"hdd1"', '"1"', "true", "0", "-1", "1.5"):
                path.write_text(base + "bay=" + bad)
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    config.load(path)
            path.write_text(base + 'bay=1\n[[drives]]\nserial="T"\nbay=1')
            with self.assertRaises(ValueError):
                config.load(path)
            for bad in ("nan", "true", "0", "86401", '"60"'):
                path.write_text("discovery_interval=" + bad + "\n" + base)
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    config.load(path)
            path.write_text("discovery_interval=12.5\n" + base)
            cfg = config.load(path)
            self.assertEqual(cfg.discovery_interval, 12.5)
            self.assertIsNone(cfg.drives[0].bay)
