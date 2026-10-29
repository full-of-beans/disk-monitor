"""Read-only bounded hardware probes and pure conservative parsers."""

import json
import re
import subprocess
from pathlib import Path


def run(argv, timeout):
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
    )


def spindle(result):
    matches = re.findall(
        r"^[ \t]*drive state is:[ \t]*([a-z][a-z0-9 /_-]*)[ \t]*$",
        result.stdout.lower(),
        re.MULTILINE,
    )
    raw = (
        " ".join(matches[0].split())
        if result.returncode == 0 and len(matches) == 1
        else None
    )
    return (
        "unknown" if raw is None else "standby" if raw == "standby" else "active",
        raw,
    )


def smart_document(result):
    if result.returncode == 3:
        raise ValueError("standby")
    if result.returncode < 0 or result.returncode & 7:
        raise ValueError("smart_command_failed")
    document = json.loads(result.stdout)
    if not isinstance(document, dict):
        raise ValueError("invalid_smart_document")
    return document


def temperature(document):
    obj = document.get("temperature")
    value = obj.get("current") if isinstance(obj, dict) else None
    if type(value) is not int or not -127 <= value <= 127:
        raise ValueError("temperature_unavailable")
    return value


def self_test(document):
    try:
        code = document["ata_smart_data"]["self_test"]["status"]["value"]
    except (KeyError, TypeError):
        raise ValueError("self_test_unavailable") from None
    if (
        type(code) is not int
        or not 0 <= code <= 255
        or code >> 4 not in (*range(9), 15)
    ):
        raise ValueError("self_test_unavailable")
    return "active" if code >> 4 == 15 else "inactive"


def io_counter(device, sys_block=Path("/sys/block")):
    fields = tuple(
        int(f) for f in (sys_block / device.name / "stat").read_text().split()
    )
    if len(fields) < 11 or any(f < 0 for f in fields):
        raise ValueError("invalid_io_counters")
    # Keep request/sector counters too: short real I/O may add no io_ticks.
    return fields[9], fields[:8] + fields[11:15]


def observe(kind, device, runner=run):
    try:
        if kind == "spindle":
            value, raw = spindle(runner(["hdparm", "-C", device.path], 10))
            return (
                value,
                None if raw is not None else "power_unavailable",
                {"raw_state": raw},
            )
        args = ["smartctl", "-d", "sat", "-n", "standby,3,5"]
        args += (
            ["-l", "scttempsts", "--json"]
            if kind == "temperature"
            else ["-c", "--json"]
        )
        doc = smart_document(runner(args + [device.path], 3))
        # If firmware supplies identity despite the narrow query, reject a
        # contradiction rather than accepting telemetry for another drive.
        if "serial_number" in doc and doc["serial_number"] not in device.identities:
            return None, "identity_changed", {}
        # No -i: activity is read from capabilities, not a historical test log.
        value = temperature(doc) if kind == "temperature" else self_test(doc)
        # Current running-test status is activity evidence; a temperature or
        # historical/inactive test value alone may be cached and is not.
        return (
            value,
            None,
            {"confirms_active": kind == "self_test" and value == "active"},
        )
    except ValueError as error:
        reason = str(error)
        if reason not in {
            "standby",
            "smart_command_failed",
            "invalid_smart_document",
            "temperature_unavailable",
            "self_test_unavailable",
        }:
            reason = "invalid_response"
        return None, reason, {}
    except subprocess.TimeoutExpired:
        return None, "timeout", {}
    except (OSError, subprocess.SubprocessError, TypeError, UnicodeError):
        return None, "probe_unavailable", {}
