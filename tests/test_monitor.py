"""Hardware-free contracts. These fixtures do not establish USB bridge behavior."""

import contextlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from disk_monitor import cli, config, discovery, probes, service, state


def result(text="", code=0):
    return subprocess.CompletedProcess([], code, text, "")


class Configuration(unittest.TestCase):
    def test_example(self):
        cfg = config.load("config/example.toml")
        self.assertEqual(cfg.intervals, config.DEFAULTS)
        self.assertEqual(cfg.drives[0].bay, 1)

    def test_validation(self):
        base = '[[drives]]\nserial="S"\n'
        invalid = [
            "",
            base + "typo=1",
            base + 'identity_aliases="S"',
            base + 'identity_aliases=["S"]',
            base + '\n[[drives]]\nserial="S"',
            base + 'bay=" "',
            "[intervals]\nio=nan\n" + base,
            "[intervals]\nio=true\n" + base,
            "[intervals]\nio=0\n" + base,
            "[intervals]\nio=86401\n" + base,
            "[intervals]\nunknown=2\n" + base,
            '[output]\npath="relative"\n' + base,
            '[output]\npath="/run/../etc/test"\n' + base,
            base + '\n[[drives]]\nserial="T"\nidentity_aliases=["S"]',
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            for text in invalid:
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(ValueError):
                        config.load(path)
            path.write_text("[intervals]\nio=2.5\n" + base)
            self.assertEqual(config.load(path).intervals["io"], 2.5)


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sys = self.root / "sys"
        self.udev = self.root / "udev"
        self.links = self.root / "by-id"
        for path in (self.sys, self.udev, self.links):
            path.mkdir()

    def disk(self, name="sda", serial="S", seq="1", dev="8:0"):
        path = self.sys / name
        (path / "device").mkdir(parents=True, exist_ok=True)
        (path / "device/serial").write_text(serial)
        (path / "diskseq").write_text(seq)
        (path / "dev").write_text(dev)
        return path

    def scan(self):
        return discovery.discover(self.sys, self.udev, self.links)

    def test_kernel_udev_and_by_id_exact_aliases(self):
        self.disk(serial="bridge")
        (self.udev / "b8:0").write_text(
            "E:ID_SERIAL_SHORT=actual\nE:ID_SCSI_SERIAL=scsi\nE:ID_SERIAL=full\n"
        )
        (self.links / "ata-model_actual").symlink_to("/dev/sda")
        (self.links / "ata-model_actual-part1").symlink_to("/dev/sda")
        disk = self.scan()[0]
        self.assertEqual(
            disk.identities,
            frozenset(("bridge", "actual", "scsi", "full", "ata-model_actual")),
        )
        self.assertEqual(
            discovery.resolve((config.Drive("actual"),), [disk])["actual"][0], "present"
        )
        self.assertEqual(
            discovery.resolve((config.Drive("act"),), [disk])["act"][0], "missing"
        )

    def test_missing_ambiguous_and_conflicting_claims(self):
        self.disk()
        self.disk("sdb", dev="8:16")
        disks = self.scan()
        self.assertEqual(
            discovery.resolve((config.Drive("S"),), disks)["S"], ("ambiguous", None)
        )
        one = disks[:1]
        rows = discovery.resolve(
            (config.Drive("S"), config.Drive("T", identity_aliases=("S",))), one
        )
        self.assertTrue(all(v == ("ambiguous", None) for v in rows.values()))
        self.assertEqual(
            discovery.resolve((config.Drive("new"),), disks)["new"], ("missing", None)
        )

    def test_generation_changes_and_unreadable_metadata(self):
        path = self.disk()
        first = self.scan()[0]
        self.assertTrue(discovery.same_generation(first, self.sys))
        (path / "diskseq").write_text("2")
        self.assertFalse(discovery.same_generation(first, self.sys))
        self.assertNotEqual(first, self.scan()[0])
        (path / "device/serial").unlink()
        self.assertEqual(self.scan()[0].identities, frozenset())
        (path / "diskseq").unlink()
        self.assertFalse(discovery.same_generation(first, self.sys))
        with self.assertRaises(OSError):
            self.scan()
        (path / "diskseq").write_text("2")
        self.disk("sda1")
        self.assertEqual(len(self.scan()), 1)


class Probes(unittest.TestCase):
    def test_conservative_spindle(self):
        for text, expected in [
            ("drive state is: standby", "standby"),
            ("drive state is: unknown", "active"),
            (" DRIVE STATE IS: IDLE_B ", "active"),
            ("drive state is: sleeping", "active"),
            ("error: drive state is: standby", "unknown"),
            ("drive state is:", "unknown"),
            ("drive state is: standby\ndrive state is: standby", "unknown"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(probes.spindle(result(text))[0], expected)
                self.assertEqual(probes.spindle(result(text, 1))[0], "unknown")

    def test_smart_exit_masks(self):
        for code in (0, 8, 16, 32, 64, 128):
            self.assertEqual(probes.smart_document(result("{}", code)), {})
        for code in (-9, 1, 2, 3, 4, 5, 7):
            with self.assertRaises(ValueError):
                probes.smart_document(result("{}", code))
        for text in ("[]", "null", "{"):
            with self.assertRaises(ValueError):
                probes.smart_document(result(text))

    def test_temperature_signed_range_and_sentinel(self):
        for value in (-127, -1, 0, 42, 127):
            self.assertEqual(
                probes.temperature({"temperature": {"current": value}}), value
            )
        for value in (-128, 128, True, None, "42", 42.5):
            with self.assertRaises(ValueError):
                probes.temperature({"temperature": {"current": value}})

    def test_self_test_current_status(self):
        for value in (0, 16, 128, 240, 249, True, -1, 256, 144, None):
            doc = {"ata_smart_data": {"self_test": {"status": {"value": value}}}}
            if (
                type(value) is int
                and 0 <= value <= 255
                and value >> 4 in (*range(9), 15)
            ):
                self.assertEqual(
                    probes.self_test(doc), "active" if value >> 4 == 15 else "inactive"
                )
            else:
                with self.assertRaises(ValueError):
                    probes.self_test(doc)
        with self.assertRaises(ValueError):
            probes.self_test({})

    def test_exact_no_wake_commands_and_failures(self):
        disk = discovery.Device("sda", frozenset(("S",)), ("p", "8:0", "1"))
        runner = Mock(return_value=result('{"temperature":{"current":42}}'))
        self.assertEqual(probes.observe("temperature", disk, runner)[:2], (42, None))
        self.assertEqual(
            runner.call_args.args,
            (
                [
                    "smartctl",
                    "-d",
                    "sat",
                    "-n",
                    "standby,3,5",
                    "-l",
                    "scttempsts",
                    "--json",
                    "/dev/sda",
                ],
                3,
            ),
        )
        runner.return_value = result(
            '{"ata_smart_data":{"self_test":{"status":{"value":240}}}}'
        )
        self.assertEqual(probes.observe("self_test", disk, runner)[0], "active")
        self.assertTrue(probes.observe("self_test", disk, runner)[2]["confirms_active"])
        self.assertNotIn("-i", runner.call_args.args[0])
        self.assertIn("-c", runner.call_args.args[0])
        for failure in (
            OSError(),
            subprocess.TimeoutExpired("x", 3),
            subprocess.SubprocessError(),
            UnicodeError(),
        ):
            runner.side_effect = failure
            self.assertIsNone(probes.observe("temperature", disk, runner)[0])
        with patch.object(subprocess, "run", return_value=result()) as run:
            probes.run(["hdparm", "-C", "/dev/sda"], 10)
            self.assertEqual(run.call_args.kwargs["timeout"], 10)
            self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_io_stat_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sda").mkdir()
            path = root / "sda/stat"
            disk = discovery.Device("sda", frozenset(), ("p", "8:0", "1"))
            path.write_text("1 2 3 4 5 6 7 8 9 123 999")
            self.assertEqual(probes.io_counter(disk, root), (123, tuple(range(1, 9))))
            for text in ("1 2", "x", "1 2 3 4 5 6 7 8 9 -1 0"):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    probes.io_counter(disk, root)


class Polling(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.disk = discovery.Device("sda", frozenset(("S",)), ("p", "8:0", "1"))
        self.visible = [self.disk]
        self.counter = 0
        self.probe = Mock(return_value=("active", None, {}))
        self.cfg = config.Config((config.Drive("S", 1),), dict(config.DEFAULTS))
        self.monitor = service.Monitor(
            self.cfg,
            discoverer=lambda: self.visible,
            probe=self.probe,
            counter=lambda _: (self.counter, (self.counter,)),
            validator=lambda d: d in self.visible,
            clock=lambda: self.now,
            epoch=lambda: self.now,
        )
        self.addCleanup(self.monitor.close)

    def settle(self):
        for future, *_ in list(self.monitor.pending.values()):
            future.result(timeout=2)
        return self.monitor.tick()["disks"]["S"]

    def test_independent_deadlines_and_io_average(self):
        self.monitor.tick()
        for _ in range(3):
            row = self.settle()
        self.assertEqual(self.probe.call_count, 3)
        self.assertEqual(row["observations"]["io"]["error"], "baseline_required")
        self.now += 10
        self.counter = 5000
        row = self.monitor.tick()["disks"]["S"]
        self.assertEqual(row["observations"]["io"]["value"], 50)
        self.assertEqual(row["observations"]["spindle"]["observed_at"], 100)
        self.assertEqual(self.probe.call_count, 3)
        self.now += 20
        self.monitor.tick()
        self.settle()
        self.assertEqual(self.probe.call_args.args[0], "spindle")

    def test_reset_gap_and_io_failure(self):
        self.monitor.tick()
        self.now += 10
        self.counter = 100
        self.monitor.tick()
        self.now += 10
        self.counter = 0
        self.assertIsNone(
            self.monitor.tick()["disks"]["S"]["observations"]["io"]["value"]
        )
        self.now += 100
        self.assertIsNone(
            self.monitor.tick()["disks"]["S"]["observations"]["io"]["value"]
        )
        self.monitor.counter = Mock(side_effect=OSError())
        self.now += 10
        self.assertEqual(
            self.monitor.tick()["disks"]["S"]["observations"]["io"]["error"],
            "counters_unavailable",
        )

    def test_reconnect_drops_inflight_and_all_old_evidence(self):
        release = threading.Event()
        started = threading.Event()

        def slow(*_):
            started.set()
            release.wait(2)
            return "standby", None, {}

        self.monitor.probe = slow
        self.monitor.tick()
        self.assertTrue(started.wait(1))
        self.visible = []
        self.now += 60
        row = self.monitor.tick()["disks"]["S"]
        self.assertIn(row["presence"], ("missing", "unknown"))
        self.visible = [discovery.Device("sda", frozenset(("S",)), ("p", "8:0", "2"))]
        self.now += 60
        self.monitor.tick()
        release.set()
        row = self.settle()
        self.assertIsNone(row["observations"]["spindle"]["observed_at"])
        self.assertIsNone(row["observations"]["io"]["value"])

    def test_slow_smart_does_not_block_io(self):
        release = threading.Event()
        self.monitor.probe = lambda *_: (release.wait(2), None, {})
        self.monitor.tick()
        self.now += 10
        self.counter = 1000
        row = self.monitor.tick()["disks"]["S"]
        release.set()
        self.assertEqual(row["observations"]["io"]["value"], 10)

    def test_renumbering_resets_io_and_new_identity_never_inherits(self):
        self.monitor.tick()
        self.now += 10
        self.counter = 1000
        self.monitor.tick()
        self.visible = [discovery.Device("sdb", frozenset(("S",)), ("q", "8:16", "2"))]
        self.now += 60
        row = self.monitor.tick()["disks"]["S"]
        self.assertEqual(row["device"], "/dev/sdb")
        self.assertIsNone(row["observations"]["io"]["value"])
        self.visible = [
            discovery.Device("sdb", frozenset(("replacement",)), ("q", "8:16", "3"))
        ]
        self.now += 60
        row = self.monitor.tick()["disks"]["S"]
        self.assertEqual(row["presence"], "missing")
        self.assertIsNone(row["observations"]["temperature"]["last_success_at"])

    def test_other_drive_progresses_while_one_command_waits(self):
        release = threading.Event()
        other = discovery.Device("sdb", frozenset(("T",)), ("q", "8:16", "2"))
        cfg = config.Config(
            (config.Drive("S"), config.Drive("T")), dict(config.DEFAULTS)
        )

        def probe(kind, disk):
            if disk.name == "sda":
                release.wait(2)
            return "active", None, {}

        monitor = service.Monitor(
            cfg,
            discoverer=lambda: [self.disk, other],
            probe=probe,
            counter=lambda _: (0, (0,)),
            validator=lambda d: True,
            clock=lambda: self.now,
            epoch=lambda: self.now,
        )
        try:
            monitor.tick()
            monitor.pending["T"][0].result(timeout=1)
            rows = monitor.tick()["disks"]
            self.assertIsNone(rows["S"]["observations"]["spindle"]["observed_at"])
            self.assertEqual(rows["T"]["observations"]["spindle"]["value"], "active")
        finally:
            release.set()
            monitor.close()

    def test_failed_probe_preserves_last_success_without_refresh(self):
        self.monitor.record("S", "temperature", 42, None)
        self.now += 60
        self.monitor.record("S", "temperature", None, "timeout")
        obs = self.monitor.rows["S"]["observations"]["temperature"]
        self.assertIsNone(obs["observed_at"])
        self.assertEqual((obs["last_value"], obs["last_success_at"]), (42, 100))
        self.assertEqual(obs["attempted_at"], 160)

    def test_discovery_failure_unknown_and_precommand_recheck(self):
        self.monitor.discoverer = Mock(side_effect=OSError())
        self.assertEqual(self.monitor.tick()["disks"]["S"]["presence"], "unknown")
        self.assertEqual(
            self.monitor.hardware("S", self.disk, "spindle")[1], "identity_changed"
        )
        self.probe.assert_not_called()


class PublicationAndCLI(unittest.TestCase):
    def test_atomic_permissions_failure_cleanup_and_freshness(self):
        doc = {
            "schema_version": state.SCHEMA_VERSION,
            "published_at": 100,
            "disks": {
                "S": {
                    "serial": "S",
                    "presence": "present",
                    "observations": {
                        k: dict(service.empty(v, k), observed_at=95, error=None)
                        for k, v in config.DEFAULTS.items()
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            state.publish(path, doc)
            self.assertEqual(json.loads(path.read_text()), doc)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            with patch.object(state.os, "replace", side_effect=OSError()):
                with self.assertRaises(OSError):
                    state.publish(path, {})
            self.assertEqual(json.loads(path.read_text()), doc)
            self.assertEqual(list(Path(directory).glob(".state-*")), [])
            self.assertTrue(
                state.with_freshness(doc, 100)["disks"]["S"]["observations"]["io"][
                    "fresh"
                ]
            )
            for now, fresh in ((90, False), (106, True), (200, False)):
                refreshed = state.with_freshness(doc, now)
                self.assertNotIn("service_fresh", refreshed)
                self.assertEqual(
                    refreshed["disks"]["S"]["observations"]["io"]["fresh"], fresh
                )
            with self.assertRaises(ValueError):
                state.with_freshness({"schema_version": 9})

    def test_cli_validate_discovery_status_failures(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                cli.main(["--config", "config/example.toml", "validate"]), 0
            )
            self.assertEqual(cli.main(["--config", "/missing", "validate"]), 1)
            with (
                patch.object(cli, "discover", return_value=[]),
                patch.object(probes, "run", side_effect=AssertionError()),
            ):
                self.assertEqual(cli.main(["discover"]), 0)
            self.assertEqual(cli.main(["--config", "config/example.toml", "status"]), 1)

    def test_malformed_state_is_rejected_without_traceback(self):
        for document in (
            {"schema_version": True},
            {"schema_version": state.SCHEMA_VERSION},
            {"schema_version": state.SCHEMA_VERSION, "published_at": True, "disks": {}},
            {
                "schema_version": state.SCHEMA_VERSION,
                "published_at": 1,
                "disks": {"S": []},
            },
            {
                "schema_version": state.SCHEMA_VERSION,
                "published_at": 1,
                "disks": {
                    "S": {"serial": "S", "presence": "present", "observations": []}
                },
            },
        ):
            with self.subTest(document=document), self.assertRaises(ValueError):
                state.with_freshness(document)

    def test_single_writer_and_clean_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = config.Config(
                (config.Drive("S"),), dict(config.DEFAULTS), Path(directory) / "state"
            )
            stop = threading.Event()
            stop.set()
            service.serve(cfg, stop)
            import fcntl

            with open(str(cfg.output) + ".lock", "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self.assertRaises(BlockingIOError):
                    service.serve(cfg, stop)


class InstalledLifecycle(unittest.TestCase):
    def test_real_command_timeout(self):
        import sys

        with self.assertRaises(subprocess.TimeoutExpired):
            probes.run([sys.executable, "-c", "import time; time.sleep(5)"], 0.05)

    def test_install_foreground_sigterm_restart_and_config_preservation(self):
        import os
        import time

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                ["sh", "install.sh"],
                env=dict(os.environ, DESTDIR=directory),
                check=True,
                capture_output=True,
            )
            cfg = root / "etc/disk-monitor/config.toml"
            output = root / "run/state.json"
            cfg.write_text(
                '[output]\npath="'
                + str(output)
                + '"\n[[drives]]\nserial="NO_MATCH_TEST_ONLY"\n'
            )
            original = cfg.read_bytes()
            subprocess.run(
                ["sh", "install.sh"],
                env=dict(os.environ, DESTDIR=directory),
                check=True,
                capture_output=True,
            )
            self.assertEqual(cfg.read_bytes(), original)
            launcher = root / "usr/local/sbin/disk-monitor"
            check = subprocess.run(
                [str(launcher), "--config", str(cfg), "validate"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(check.returncode, 0, check.stderr)
            for _ in range(2):
                if output.exists():
                    output.unlink()
                child = subprocess.Popen(
                    [str(launcher), "--config", str(cfg), "run"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 3
                    while (
                        not output.exists()
                        and child.poll() is None
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    self.assertTrue(output.exists())
                    status = subprocess.run(
                        [str(launcher), "--config", str(cfg), "status"],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(status.returncode, 0, status.stderr)
                    row = json.loads(status.stdout)["disks"]["NO_MATCH_TEST_ONLY"]
                    self.assertIn(row["presence"], ("missing", "unknown"))
                    self.assertEqual(
                        row["observations"]["self_test"]["value"], "unknown"
                    )
                finally:
                    child.terminate()
                    stdout, stderr = child.communicate(timeout=12)
                    self.assertEqual(child.returncode, 0, stdout + stderr)
            unit = (root / "etc/systemd/system/disk-monitor.service").read_text()
            self.assertIn("ExecStart=/usr/local/sbin/disk-monitor", unit)
            self.assertIn("TimeoutStopSec=15", unit)
            import shutil

            if shutil.which("systemd-analyze"):
                check_unit = root / "disk-monitor.service"
                check_unit.write_text(
                    unit.replace("/usr/local/sbin/disk-monitor", str(launcher))
                )
                verified = subprocess.run(
                    ["systemd-analyze", "verify", "--man=no", str(check_unit)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(verified.returncode, 0, verified.stderr)


if __name__ == "__main__":
    unittest.main()
