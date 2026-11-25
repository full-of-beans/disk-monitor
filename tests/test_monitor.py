import io
import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from disk_monitor.cli import main
from disk_monitor.config import Config, Drive, Hook, load
from disk_monitor.counters import FIELDS, METRICS, metrics, sample
from disk_monitor.discovery import (
    Device,
    Generation,
    discover,
    resolve,
    same_generation,
)
from disk_monitor.probes import Smart, observe
from disk_monitor.service import Monitor, schedule, serve
from disk_monitor.state import encode, publish


def device(serial="S", name="sda", seq=12, transport="sat", aliases=()):
    return Device(
        name,
        frozenset((serial, *aliases)),
        Generation(f"/sys/test/{name}", "8:0", seq, 123),
        transport,
        aliases,
        serial,
    )


class ConfigTests(unittest.TestCase):
    def load(self, body):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.toml"
            path.write_text(body)
            return load(path)

    def test_defaults_and_large_inventory(self):
        config = self.load("".join(f'[[drives]]\nserial="S{i}"\n' for i in range(64)))
        self.assertEqual(len(config.drives), 64)
        self.assertEqual(config.interval, 60)
        self.assertEqual(config.output, Path("/run/disk-monitor/state.json"))
        self.assertEqual(config.counters, Path("/run/disk-monitor/counters.json"))
        self.assertEqual(config.post_hooks, ())

    def test_options(self):
        config = self.load("""
interval = 12.5
[[drives]]
serial = "S"
identity_aliases = ["ata-M_S"]
[output]
path = "/run/foo/state.json"
counters_path = "/run/foo/counters.json"
[[post_hooks]]
name = "first"
command = ["/opt/receiver", "", "literal; argument"]
[[post_hooks]]
name = "second"
command = ["receiver"]
timeout = 0.5
""")
        self.assertEqual(config.drives, (Drive("S", ("ata-M_S",)),))
        self.assertEqual(config.interval, 12.5)
        self.assertEqual(config.counters, Path("/run/foo/counters.json"))
        self.assertEqual(
            config.post_hooks[0],
            Hook("first", ("/opt/receiver", "", "literal; argument")),
        )
        self.assertEqual(config.post_hooks[1].timeout, 0.5)

    def test_invalid_drive_configurations(self):
        for document in (
            "",
            "drives=[]",
            "drives=1",
            "[[drives]]",
            '[[drives]]\nserial=""',
            '[[drives]]\nserial=" S"',
            '[[drives]]\nserial="S"\nbay=1',
            '[[drives]]\nserial="S"\nidentity_aliases="ata-M_S"',
            '[[drives]]\nserial="S"\nidentity_aliases=["S"]',
            '[[drives]]\nserial="S"\n[[drives]]\nserial="S"',
            '[[drives]]\nserial="S"\nidentity_aliases=["T"]\n[[drives]]\nserial="T"',
            '[[drives]]\nserial="S"\nidentity_aliases=["A"]\n[[drives]]\nserial="T"\nidentity_aliases=["A"]',
        ):
            with self.subTest(document=document), self.assertRaises(ValueError):
                self.load(document)

    def test_invalid_global_options(self):
        for option in (
            "interval=true",
            "interval=nan",
            "interval=inf",
            "interval=-1",
            'interval="60"',
            "interval=86401",
            "discovery_interval=60",
            "post_hooks=true",
            "intervals={io=10}",
            'output={path="state.json"}',
            'output={path="/run/a/../state.json"}',
            'output={path="/run/disk-monitor/counters.json"}',
            'output={path="/run/a.lock"}',
        ):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.load(option + '\n[[drives]]\nserial="S"')

    def test_invalid_hooks(self):
        for row in (
            'command=["x"]',
            'name="bad name"\ncommand=["x"]',
            'name="x"\ncommand="x"',
            'name="x"\ncommand=[]',
            'name="x"\ncommand=[42]',
            'name="x"\ncommand=[" "]',
            'name="x"\ncommand=["x"]\ntimeout=nan',
            'name="x"\ncommand=["x"]\ntimeout=0',
            'name="x"\ncommand=["x"]\ntimeout=true',
            'name="x"\ncommand=["x"]\ntimeout=301',
            'name="x"\ncommand=["x"]\ninterval=60',
            'name="x"\ncommand=["x"]\n[[post_hooks]]\nname="x"\ncommand=["y"]',
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.load('[[drives]]\nserial="S"\n[[post_hooks]]\n' + row)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sys_block, self.udev, self.by_id, self.dev = [
            self.root / name for name in ("sys/block", "udev", "by-id", "dev")
        ]
        for path in (self.sys_block, self.udev, self.by_id, self.dev):
            path.mkdir(parents=True)

    def block(self, name, bus, serial, minor, rotary=1, ata=True, seq=12, kind=0):
        target = self.root / "devices" / bus / "host1/target1/block" / name
        for file, value in (
            ("dev", f"8:{minor}"),
            ("queue/rotational", str(rotary)),
            ("device/type", str(kind)),
            ("device/vendor", "ATA" if bus == "ata1" else "Other"),
        ):
            item = target / file
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_text(value)
        if seq is not None:
            (target / "diskseq").write_text(str(seq))
        (self.udev / f"b8:{minor}").write_text(
            f"E:ID_SERIAL_SHORT={serial}\nE:ID_ATA={int(ata)}\n"
        )
        (self.sys_block / name).symlink_to(target)
        (self.dev / name).touch()
        return target

    def discover(self):
        return discover(self.sys_block, self.udev, self.by_id, self.dev)

    def test_native_sat_aliases_and_supported_types(self):
        self.block("sda", "ata1", "NATIVE", 0)
        self.block("sdb", "usb2", "USB", 16)
        self.block("sdc", "ata1", "SSD", 32, rotary=0)
        self.block("sdd", "sas", "SAS", 48, ata=False)
        self.block("sde", "raid", "RAID", 64, ata=False)
        self.block("sdf", "usb2", "UNVERIFIED", 80, ata=False)
        self.block("sdg", "usb2", "OPTICAL", 96, kind=5)
        (self.by_id / "ata-USB").symlink_to(self.dev / "sdb")
        (self.by_id / "ata-USB-part1").symlink_to(self.dev / "sdb")
        (self.by_id / "broken").symlink_to(self.dev / "sdh")
        (self.by_id / "loop").symlink_to("loop")
        from disk_monitor.discovery import reverse_aliases

        with patch(
            "disk_monitor.discovery.reverse_aliases", wraps=reverse_aliases
        ) as scan:
            devices = self.discover()
        scan.assert_called_once_with(self.by_id, self.dev)
        self.assertEqual(
            [(d.name, d.transport, d.by_id) for d in devices],
            [("sda", "ata", ()), ("sdb", "sat", ("ata-USB",)), ("sdf", None, ())],
        )
        registry = resolve(
            (
                Drive("NATIVE"),
                Drive("alias", ("ata-USB",)),
                Drive("UNVERIFIED"),
                Drive("ata-USB"),
                Drive("US"),
                Drive("SSD"),
            ),
            devices,
        )
        self.assertEqual(
            [item[0] for item in registry.values()],
            ["present", "present", "unknown", "missing", "missing", "missing"],
        )

    def test_ambiguous_matches_and_multiple_claimants(self):
        one = device("S", aliases=("alias",))
        two = device("S", "sdb")
        self.assertEqual(resolve((Drive("S"),), [one, two])["S"], ("ambiguous", None))
        result = resolve((Drive("S"), Drive("T", ("alias",))), [one, two])
        self.assertEqual(result, {"S": ("ambiguous", None), "T": ("ambiguous", None)})

    def test_generation_change_and_optional_diskseq(self):
        target = self.block("sda", "ata1", "S", 0, seq=None)
        before = self.discover()[0]
        self.assertIsNone(before.generation.diskseq)
        self.assertTrue(same_generation(before, self.sys_block))
        (target / "diskseq").write_text("42")
        self.assertFalse(same_generation(before, self.sys_block))
        self.assertEqual(self.discover()[0].generation.diskseq, 42)

    def test_incomplete_or_changing_metadata(self):
        target = self.block("sda", "ata1", "S", 0)
        with patch(
            "disk_monitor.discovery.reverse_aliases",
            side_effect=lambda *_: ((target / "diskseq").write_text("13"), {})[1],
        ):
            with self.assertRaisesRegex(OSError, "changed"):
                self.discover()
        (self.udev / "b8:0").write_text("E:DISKSEQ=12\nE:ID_SERIAL_SHORT=S\n")
        with self.assertRaisesRegex(OSError, "different disk generation"):
            self.discover()
        (target / "dev").unlink()
        with self.assertRaisesRegex(OSError, "incomplete"):
            self.discover()


def smart_result(
    rc=0, power="Power mode is:    ACTIVE or IDLE", temp=34, code=0, extra=None
):
    # Field layout and strings follow the smartmontools 7.3 ATA renderer.
    document = {
        "json_format_version": [1, 0],
        "smartctl": {
            "version": [7, 3],
            "exit_status": rc,
            "output": [power] if power else [],
        },
    }
    if temp is not None:
        document["ata_sct_status"] = {"temperature": {"current": temp}}
    if code is not None:
        document["ata_smart_data"] = {"self_test": {"status": {"value": code}}}
    document.update(extra or {})
    return subprocess.CompletedProcess([], rc, json.dumps(document), "")


class SmartTests(unittest.TestCase):
    def test_single_explicit_protected_command_for_each_transport(self):
        for transport in ("ata", "sat"):
            runner = Mock(return_value=smart_result(code=0xF9))
            self.assertEqual(
                observe(device(transport=transport), runner),
                Smart("active", 34, "active"),
            )
            runner.assert_called_once_with(
                [
                    "smartctl",
                    "-d",
                    transport,
                    "-n",
                    "standby,3,5",
                    "-i",
                    "-c",
                    "-l",
                    "scttempsts",
                    "--json=o",
                    "/dev/sda",
                ],
                3,
            )
        runner.reset_mock()
        self.assertEqual(observe(device(transport=None), runner).spindle, "unknown")
        runner.assert_not_called()

    def test_power_evidence_and_exit_code_collisions(self):
        for rc, line, expected in (
            (3, "Device is in STANDBY mode, exit(3)", "standby"),
            (3, "Device is in STANDBY_Y mode, exit(3)", "standby"),
            (3, "Device is in SLEEP mode, exit(3)", "unknown"),
            (3, "Device is in IDLE_C mode, exit(3)", "unknown"),
            (3, "Smartctl open device: /dev/sda failed", "unknown"),
            (5, "CHECK POWER MODE not implemented, exit(5)", "unknown"),
            (5, "CHECK POWER MODE returned unknown value 0x06, exit(5)", "unknown"),
            (5, "Device is in STANDBY mode, exit(3)", "unknown"),
            (0, "CHECK POWER MODE not implemented, ignoring -n option", "unknown"),
            (0, "Device is in STANDBY mode, exit(3)", "unknown"),
            (0, "Power mode was:   ACTIVE_NV_DOWN", "unknown"),
            (0, None, "unknown"),
        ):
            with self.subTest(rc=rc, line=line):
                runner = Mock(return_value=smart_result(rc, line))
                result = observe(device(), runner)
                runner.assert_called_once()
                self.assertEqual(result.spindle, expected)
                self.assertIsNone(result.temperature_c)
                self.assertEqual(result.self_test, "unknown")

    def test_idle_modes_allow_smart_data(self):
        for mode, value in zip(
            ("IDLE", "IDLE_A", "IDLE_B", "IDLE_C"), range(0x80, 0x84)
        ):
            for result in (
                smart_result(power=f"Power mode was:   {mode}"),
                smart_result(
                    power=None, extra={"power_mode": {"name": mode, "ata_value": value}}
                ),
            ):
                with self.subTest(mode=mode, output=result.stdout):
                    self.assertEqual(
                        observe(device(), Mock(return_value=result)),
                        Smart("active", 34, "inactive"),
                    )

    def test_conflicting_power_evidence_leaves_smart_unknown(self):
        for rc, power in (
            (0, "Power mode was:   IDLE_C"),
            (3, "Device is in STANDBY_Y mode, exit(3)"),
        ):
            with self.subTest(rc=rc):
                result = smart_result(
                    rc,
                    power,
                    extra={"power_mode": {"name": "STANDBY", "ata_value": 0}},
                )
                self.assertEqual(
                    observe(device(), Mock(return_value=result)),
                    Smart(error="conflicting ATA power-mode evidence"),
                )

    def test_independent_smart_fields_and_high_exit_bits(self):
        for rc, temp, test_code, expected_temp, expected_test in (
            (4, None, 0xF4, None, "active"),
            (4, 37, None, 37, "unknown"),
            (8, 30, 0x70, 30, "inactive"),
            (0, -128, 0x9F, None, "unknown"),
            (0, True, True, None, "unknown"),
            (0, 0, 0xFF, 0, "unknown"),
        ):
            with self.subTest(rc=rc, temp=temp, test_code=test_code):
                result = observe(
                    device(),
                    Mock(return_value=smart_result(rc, temp=temp, code=test_code)),
                )
                self.assertEqual(
                    (result.spindle, result.temperature_c, result.self_test),
                    ("active", expected_temp, expected_test),
                )

    def test_structured_power_and_false_samsung_active(self):
        result = smart_result(
            power="with unknown result or self-test in",
            code=0xF0,
            extra={"power_mode": {"name": "ACTIVE or IDLE", "ata_value": 255}},
        )
        self.assertEqual(
            observe(device(), Mock(return_value=result)).self_test, "unknown"
        )

    def test_standby_structured_message(self):
        result = smart_result(
            3,
            power=None,
            temp=None,
            code=None,
            extra={
                "smartctl": {
                    "version": [7, 3],
                    "exit_status": 3,
                    "messages": [
                        {
                            "string": "Device is in STANDBY mode, exit(3)",
                            "severity": "information",
                        }
                    ],
                }
            },
        )
        self.assertEqual(observe(device(), Mock(return_value=result)), Smart("standby"))

    def test_failure_does_not_retry(self):
        for bad in (
            subprocess.TimeoutExpired("smartctl", 3),
            FileNotFoundError("smartctl"),
            subprocess.CompletedProcess([], 0, "{bad JSON", ""),
            subprocess.CompletedProcess([], 0, "[]", ""),
            smart_result(extra={"smartctl": {"version": [7, 2], "exit_status": 0}}),
            smart_result(extra={"smartctl": {"version": [7, 3], "exit_status": 3}}),
            smart_result(
                5,
                "CHECK POWER MODE returned unknown value 0x06, exit(5)",
                extra={"power_mode": {"name": "IDLE_C", "ata_value": 0x83}},
            ),
            smart_result(extra={"serial_number": "OTHER"}),
        ):
            with self.subTest(bad=bad):
                runner = (
                    Mock(side_effect=bad)
                    if isinstance(bad, Exception)
                    else Mock(return_value=bad)
                )
                result = observe(device(), runner)
                runner.assert_called_once()
                self.assertEqual(
                    (result.spindle, result.temperature_c, result.self_test),
                    ("unknown", None, "unknown"),
                )


class CounterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.stat = self.root / "sda/stat"
        self.stat.parent.mkdir()

    def sample(self, fields, stamp, seq=12):
        self.stat.write_text(" ".join(map(str, fields)))
        return sample(device(seq=seq), self.root, lambda: stamp)

    def test_documented_fields_and_bad_inputs(self):
        for count in (11, 15, 17, 18):
            raw = self.sample(range(count), 800)
            self.assertEqual(raw.monotonic_ns, 800)
            self.assertEqual(raw.counters, dict(zip(FIELDS, range(count))))
        for fields in ([0] * 10, [0] * 12, [0] * 16, [0] * 10 + [-1], [0] * 10 + ["x"]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.sample(fields, 800)
        self.stat.unlink()
        with self.assertRaises(FileNotFoundError):
            sample(device(), self.root)

    def test_mixed_workload_and_actual_elapsed(self):
        before = self.sample([10, 10, 20, 0, 40, 50, 50, 2, 5, 100, 0], 1_000_000_000)
        after = self.sample(
            [30, 12, 60, 3000, 46, 52, 80, 1502, 1, 900, 4000], 3_000_000_000
        )
        self.assertEqual(
            metrics(before, after),
            {
                "busy_percent": 40.0,
                "read_time_percent": 150.0,
                "write_time_percent": 75.0,
                "read_bytes_per_second": 10240.0,
                "write_bytes_per_second": 7680.0,
                "read_iops": 10.0,
                "write_iops": 3.0,
            },
        )

    def test_idle_and_directional_workloads(self):
        before = self.sample([0] * 11, 0)
        for read, write in ((0, 0), (6, 0), (0, 8)):
            after = self.sample(
                [read, 0, read, read, write, 0, write, write, 0, read + write, 0],
                1_000_000_000,
            )
            result = metrics(before, after)
            self.assertEqual(result["read_iops"], read)
            self.assertEqual(result["write_iops"], write)
            self.assertEqual(result["busy_percent"], (read + write) / 10)
            self.assertEqual(result["read_bytes_per_second"], 512 * read)
            self.assertEqual(result["write_bytes_per_second"], 512 * write)

    def test_reset_rollover_zero_elapsed_and_generation(self):
        before = self.sample([2**32 - 1] * 11, 100)
        for after in (
            None,
            self.sample([0] * 11, 200),
            self.sample([2**32 - 1] * 11, 100),
            self.sample([2**32 - 1] * 11, 1),
            self.sample([2**32 - 1] * 11, 200, seq=13),
        ):
            self.assertEqual(metrics(before, after), dict.fromkeys(METRICS))

    def test_each_cumulative_decrease_invalidates_all_metrics(self):
        before = self.sample([10] * 17, 0)
        for index, field in enumerate(FIELDS):
            if field == "io_in_progress":
                continue
            with self.subTest(field=field):
                values = [11] * 17
                values[index] = 9
                after = self.sample(values, 1_000_000_000)
                self.assertEqual(metrics(before, after), dict.fromkeys(METRICS))

    def test_gauge_decrease_and_absent_extensions_allow_rates(self):
        values = [11] * 17
        values[8] = 0
        for before_count, after_count in ((17, 17), (11, 17), (17, 15), (15, 11)):
            with self.subTest(layouts=(before_count, after_count)):
                before = self.sample([10] * before_count, 0)
                after = self.sample(values[:after_count], 1_000_000_000)
                self.assertEqual(
                    metrics(before, after),
                    {
                        "busy_percent": 0.1,
                        "read_time_percent": 0.1,
                        "write_time_percent": 0.1,
                        "read_bytes_per_second": 512.0,
                        "write_bytes_per_second": 512.0,
                        "read_iops": 1.0,
                        "write_iops": 1.0,
                    },
                )


class CycleTests(unittest.TestCase):
    def setUp(self):
        from disk_monitor.counters import Sample

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = Config(
            (Drive("S"), Drive("M")),
            output=self.root / "state.json",
            counters=self.root / "counters.json",
        )
        self.stop = threading.Event()
        self.stamp = "2026-11-25T15:00:00.000000Z"
        self.live = [device(), device("OTHER", "sdz")]
        self.time = 0
        self.probe = Mock(return_value=Smart("active", 34, "inactive"))

        def counter(target):
            self.time += 2_000_000_000
            values = dict.fromkeys(FIELDS, self.time // 1_000_000_000)
            return Sample(target.generation, self.time, values)

        self.options = dict(
            discoverer=lambda: self.live,
            probe=self.probe,
            counter=counter,
            validator=lambda _: True,
            node_validator=lambda _: True,
            timestamp=lambda: self.stamp,
        )
        self.monitor = self.new_monitor()

    def new_monitor(self, **options):
        monitor = Monitor(self.config, self.stop, **(self.options | options))
        self.addCleanup(monitor.close)
        return monitor

    def outputs(self):
        return (
            json.loads(self.config.output.read_text()),
            json.loads(self.config.counters.read_text()),
        )

    def test_first_second_and_clean_restart_with_existing_output(self):
        self.config.counters.write_text("this must not be read")
        self.monitor.cycle()
        state, raw = self.outputs()
        self.assertEqual(list(state), ["disks", "observed_at", "schema_version"])
        self.assertEqual(state["observed_at"], raw["updated_at"])
        self.assertEqual(state["observed_at"], self.stamp)
        self.assertEqual(state["schema_version"], 3)
        self.assertEqual(state["disks"]["S"]["io"], dict.fromkeys(METRICS))
        self.assertEqual(
            state["disks"]["M"],
            {
                "device": None,
                "presence": "missing",
                "spindle": "unknown",
                "temperature_c": None,
                "self_test": "unknown",
                "io": dict.fromkeys(METRICS),
            },
        )
        self.assertEqual(list(raw["disks"]), ["S"])
        self.assertEqual(raw["disks"]["S"]["diskseq"], 12)
        self.assertEqual(raw["disks"]["S"]["sample_monotonic_ns"], 2_000_000_000)
        self.assertEqual(raw["disks"]["S"]["counters"], dict.fromkeys(FIELDS, 2))
        self.probe.assert_called_once_with(self.live[0])

        self.probe.return_value = Smart(error="SMART timed out")
        self.monitor.cycle()
        state, _ = self.outputs()
        self.assertEqual(state["disks"]["S"]["io"]["read_iops"], 1.0)
        self.assertIsNone(state["disks"]["S"]["temperature_c"])
        self.assertEqual(state["disks"]["S"]["spindle"], "unknown")
        self.new_monitor().cycle()
        self.assertEqual(self.outputs()[0]["disks"]["S"]["io"], dict.fromkeys(METRICS))

    def test_raw_stat_publication_and_rebaseline_after_flush_counter_reset(self):
        stat = self.root / "sda" / "stat"
        stat.parent.mkdir()
        ticks = iter((1_000_000_000, 2_000_000_000, 3_000_000_000))
        monitor = self.new_monitor(
            counter=lambda target: sample(target, self.root, lambda: next(ticks))
        )

        stat.write_text(" ".join(map(str, range(10, 27))))
        monitor.cycle()
        state, raw = self.outputs()
        self.assertEqual(state["disks"]["S"]["io"], dict.fromkeys(METRICS))
        self.assertEqual(
            raw["disks"]["S"]["counters"],
            {
                "reads_completed": 10,
                "reads_merged": 11,
                "sectors_read": 12,
                "read_ms": 13,
                "writes_completed": 14,
                "writes_merged": 15,
                "sectors_written": 16,
                "write_ms": 17,
                "io_in_progress": 18,
                "io_ms": 19,
                "weighted_io_ms": 20,
                "discards_completed": 21,
                "discards_merged": 22,
                "sectors_discarded": 23,
                "discard_ms": 24,
                "flushes_completed": 25,
                "flush_ms": 26,
            },
        )

        stat.write_text(" ".join(map(str, [*range(11, 27), 1])))
        monitor.cycle()
        state, raw = self.outputs()
        self.assertEqual(state["disks"]["S"]["io"], dict.fromkeys(METRICS))
        self.assertEqual(raw["disks"]["S"]["counters"]["flush_ms"], 1)

        stat.write_text(" ".join(map(str, [*range(12, 28), 2])))
        monitor.cycle()
        state, raw = self.outputs()
        self.assertEqual(state["disks"]["S"]["io"]["read_iops"], 1.0)
        self.assertEqual(raw["disks"]["S"]["counters"]["flush_ms"], 2)

    def test_hotplug_and_path_reassignment(self):
        self.monitor.cycle()
        self.live[:] = [device("OTHER")]
        self.monitor.cycle()
        state, raw = self.outputs()
        self.assertEqual(state["disks"]["S"]["presence"], "missing")
        self.assertFalse(raw["disks"])
        self.live[:] = [device(name="sdc", seq=13), device("OTHER")]
        self.monitor.cycle()
        state, _ = self.outputs()
        self.assertEqual(state["disks"]["S"]["device"], "/dev/sdc")
        self.assertEqual(state["disks"]["S"]["io"], dict.fromkeys(METRICS))
        self.monitor.cycle()
        self.assertEqual(self.outputs()[0]["disks"]["S"]["io"]["read_iops"], 1)
        self.live[0] = device(name="sdc", seq=14)
        self.monitor.cycle()
        self.assertEqual(self.outputs()[0]["disks"]["S"]["io"], dict.fromkeys(METRICS))

    def test_discovery_failure_and_counter_failure_reset_baselines(self):
        find = Mock(
            side_effect=[
                self.live,
                self.live,
                OSError("inventory I/O"),
                self.live,
                self.live,
            ]
        )
        monitor = self.new_monitor(discoverer=find)
        monitor.cycle()
        monitor.cycle()
        self.assertEqual(self.outputs()[0]["disks"]["S"]["presence"], "unknown")
        monitor.cycle()
        self.assertEqual(self.outputs()[0]["disks"]["S"]["io"], dict.fromkeys(METRICS))

        counter = Mock(
            side_effect=[
                self.options["counter"](self.live[0]),
                OSError("read error"),
                self.options["counter"](self.live[0]),
                self.options["counter"](self.live[0]),
            ]
        )
        monitor = self.new_monitor(counter=counter)
        monitor.cycle()
        monitor.cycle()
        state, raw = self.outputs()
        self.assertEqual(state["disks"]["S"]["temperature_c"], 34)
        self.assertFalse(raw["disks"])
        monitor.cycle()
        self.assertEqual(self.outputs()[0]["disks"]["S"]["io"], dict.fromkeys(METRICS))
        monitor.cycle()
        self.assertEqual(self.outputs()[0]["disks"]["S"]["io"]["read_iops"], 1)

    def test_reject_inflight_replacement_and_ambiguous_identity(self):
        for replacement, presence in (
            ([device(seq=13)], "unknown"),
            ([device(), device(name="sdc")], "ambiguous"),
        ):
            with self.subTest(presence=presence):
                self.live[:] = [device()]

                def swap(_):
                    self.live[:] = replacement
                    return Smart("active", 70, "inactive")

                self.new_monitor(probe=swap).cycle()
                state, raw = self.outputs()
                self.assertFalse(raw["disks"])
                self.assertEqual(state["disks"]["S"]["presence"], presence)
                self.assertIsNone(state["disks"]["S"]["temperature_c"])

    def test_node_guards_drop_only_smart_when_kernel_identity_is_stable(self):
        for pre, post in ((False, False), (True, False)):
            with self.subTest(pre=pre, post=post):
                self.probe.reset_mock()
                node = Mock(side_effect=[pre, post])
                self.new_monitor(node_validator=node).cycle()
                self.assertEqual(self.probe.call_count, int(pre))
                state, raw = self.outputs()
                self.assertIsNone(state["disks"]["S"]["temperature_c"])
                self.assertIn("S", raw["disks"])

    def test_publication_order_and_failure(self):
        dispatch = Mock()
        monitor = self.monitor
        monitor.hooks.dispatch = dispatch

        def check_payload(payload):
            self.assertEqual(self.config.output.read_bytes(), payload)
            self.assertEqual(
                self.outputs()[0]["observed_at"], self.outputs()[1]["updated_at"]
            )

        dispatch.side_effect = check_payload
        with patch("disk_monitor.service.publish", wraps=publish) as writer:
            monitor.cycle()
        self.assertEqual(
            [call.args[0] for call in writer.call_args_list],
            [self.config.counters, self.config.output],
        )
        dispatch.assert_called_once()
        dispatch.reset_mock()
        with patch(
            "disk_monitor.service.publish", side_effect=[None, OSError("state blocked")]
        ):
            monitor.cycle()
        dispatch.assert_not_called()
        with patch(
            "disk_monitor.service.publish", side_effect=OSError("raw blocked")
        ) as writer:
            monitor.cycle()
        writer.assert_called_once()
        dispatch.assert_not_called()

    def test_four_concurrent_probes_and_per_drive_isolation(self):
        self.live[:] = [device(str(i), f"sd{chr(97 + i)}") for i in range(7)]
        self.config = replace(
            self.config, drives=tuple(Drive(str(i)) for i in range(7))
        )
        barrier = threading.Barrier(4, timeout=3)
        lock = threading.Lock()
        active = maximum = 0

        def probe(target):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(active, maximum)
            if int(target.ata_serial) < 4:
                barrier.wait()
            with lock:
                active -= 1
            return (
                Smart(error="test timeout")
                if target.ata_serial == "1"
                else Smart("active", 25)
            )

        self.new_monitor(probe=probe).cycle()
        self.assertEqual(maximum, 4)
        rows = self.outputs()[0]["disks"]
        self.assertEqual(len(rows), 7)
        self.assertIsNone(rows["1"]["temperature_c"])
        self.assertEqual(rows["6"]["temperature_c"], 25)

    def test_lock_covers_both_outputs(self):
        other = replace(self.config, output=self.root / "different.json")
        with patch("disk_monitor.service.schedule") as run:

            def verify_lock(*_):
                with self.assertRaisesRegex(OSError, "another disk-monitor writer"):
                    serve(other, self.stop)

            run.side_effect = verify_lock
            serve(self.config, self.stop)

    def test_atomic_replacement_keeps_old_open_file_complete(self):
        for path in (self.config.output, self.config.counters):
            publish(path, encode({"old": True}))
            with path.open() as prior:
                publish(path, encode({"new": True}))
                self.assertEqual(json.load(prior), {"old": True})
            self.assertEqual(json.loads(path.read_text()), {"new": True})
            with patch(
                "disk_monitor.state.os.replace", side_effect=OSError("test failure")
            ):
                with self.assertRaises(OSError):
                    publish(path, encode({"failed": True}))
            self.assertEqual(json.loads(path.read_text()), {"new": True})
            self.assertFalse(list(self.root.glob(".disk-monitor-*")))


class SchedulerTests(unittest.TestCase):
    def test_immediate_monotonic_start_and_skipped_overrun_slots(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        clock = Mock(
            side_effect=[
                0,
                0,
                0,
                60_000_000_000,
                185_000_000_000,
                240_000_000_000,
                242_000_000_000,
            ]
        )
        starts = []
        schedule(lambda: starts.append(clock()), 60, stop, clock=clock)
        self.assertEqual(starts, [0, 60_000_000_000, 240_000_000_000])
        self.assertEqual(
            [call.args[0] for call in stop.wait.call_args_list], [60, 55, 58]
        )

    def test_status_prints_published_document_without_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            output = Path(directory) / "state.json"
            config.write_text(f'[[drives]]\nserial="S"\n[output]\npath="{output}"\n')
            snapshot = {
                "schema_version": 3,
                "observed_at": "2026-11-25T15:00:00Z",
                "disks": {},
            }
            output.write_bytes(encode(snapshot))
            with redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(main(["--config", str(config), "status"]), 0)
            self.assertEqual(json.loads(stream.getvalue()), snapshot)
