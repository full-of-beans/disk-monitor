"""Consolidated, power-gated ATA SMART observations."""

import json
import re
import subprocess
from dataclasses import dataclass

SMART_TIMEOUT = 3
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
ACTIVE_MODES = {"ACTIVE or IDLE", "ACTIVE_NV_UP", "IDLE", "IDLE_A", "IDLE_B", "IDLE_C"}
STANDBY_MODES = {"STANDBY", "STANDBY_Y"}


@dataclass(frozen=True)
class Smart:
    spindle: str = "unknown"
    temperature_c: int | None = None
    self_test: str = "unknown"
    error: str | None = None


def run(argv, timeout):
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=ENV,
    )


def self_test(document, lines):
    try:
        code = document["ata_smart_data"]["self_test"]["status"]["value"]
    except (KeyError, TypeError):
        return "unknown"
    if type(code) is not int or not 0 <= code <= 255:
        return "unknown"
    if code >> 4 <= 8:
        return "inactive"
    if 0xF0 <= code <= 0xF9 and not any(
        "unknown result or self-test" in s for s in lines
    ):
        return "active"
    return "unknown"


def parse(result, device):
    document = json.loads(result.stdout)
    meta = document["smartctl"]
    version = meta["version"]
    rc = result.returncode
    if (
        not isinstance(version, list)
        or len(version) < 2
        or any(type(part) is not int for part in version)
        or version[:2] < [7, 3]
    ):
        return Smart(error="requires smartmontools 7.3+")
    if rc < 0 or type(meta["exit_status"]) is not int or meta["exit_status"] != rc:
        return Smart(error="invalid SMART exit status")
    lines = meta.get("output", [])
    messages = [entry["string"] for entry in meta.get("messages", [])]
    diagnostics = {*messages, *lines}
    if any("CHECK POWER MODE" in line for line in diagnostics):
        return Smart(error="unsupported or unknown ATA power check")
    reported = {
        match[1]
        for line in diagnostics
        if (match := re.fullmatch(r"Power mode (?:is|was):\s+(.+)", line))
    }
    mode = document.get("power_mode", {})
    if "power_mode" in document:
        if not isinstance(mode, dict) or not isinstance(mode.get("name"), str):
            return Smart(error="invalid ATA power-mode evidence")
        reported.add(mode["name"])
    cutoffs = {
        match[1]
        for line in diagnostics
        if (match := re.fullmatch(r"Device is in (.+) mode, exit\(3\)", line))
    }
    if len(reported | cutoffs) > 1:
        return Smart(error="conflicting ATA power-mode evidence")
    if rc == 3 and cutoffs:
        if cutoffs <= STANDBY_MODES:
            return Smart(spindle="standby")
        # A failed ATA check can also be reported by smartctl as SLEEP.
        if "SLEEP" in cutoffs:
            return Smart(error="sleep or failed ATA power check")
        return Smart(error="unexpected ATA power-mode cutoff")
    if rc & 3:
        return Smart(error=f"SMART open, identification or command-line error ({rc})")

    serial = document.get("serial_number")
    if serial is not None and (
        not isinstance(serial, str)
        or (
            serial != device.ata_serial
            if device.ata_serial
            else serial not in device.identities
        )
    ):
        return Smart(error="identity_changed")

    if cutoffs or not reported or not reported <= ACTIVE_MODES:
        return Smart(error="ATA active power state unconfirmed")
    temperature = document.get("ata_sct_status", {})
    temperature = (
        temperature.get("temperature", {}) if isinstance(temperature, dict) else {}
    )
    temperature = temperature.get("current") if isinstance(temperature, dict) else None
    if type(temperature) is not int or not -127 <= temperature <= 127:
        temperature = None
    test = self_test(document, lines)
    error = None
    if temperature is None or test == "unknown" or rc & 4:
        error = f"partial SMART data (exit {rc})"
    return Smart("active", temperature, test, error)


def observe(device, runner=run):
    if device.transport not in {"ata", "sat"}:
        return Smart(error="unsupported SMART transport")
    try:
        result = runner(
            [
                "smartctl",
                "-d",
                device.transport,
                "-n",
                "standby,3,5",
                "-i",
                "-c",
                "-l",
                "scttempsts",
                "--json=o",
                device.path,
            ],
            SMART_TIMEOUT,
        )
        return parse(result, device)
    except subprocess.TimeoutExpired:
        return Smart(error="SMART timed out")
    except (OSError, subprocess.SubprocessError) as error:
        return Smart(error=f"SMART execution failed: {error}")
    except (ValueError, TypeError, KeyError, AttributeError):
        return Smart(error="invalid SMART JSON")
