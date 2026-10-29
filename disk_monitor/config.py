"""Strict TOML configuration; identity is explicit, never a guessed sdX path."""

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULTS = {"io": 10, "spindle": 30, "temperature": 60, "self_test": 60}


@dataclass(frozen=True)
class Drive:
    serial: str
    bay: int | None = None
    identity_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    drives: tuple[Drive, ...]
    intervals: dict
    output: Path = Path("/run/disk-monitor/state.json")
    discovery_interval: float = 60


def keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"expected table with keys: {', '.join(allowed)}")


def identity(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("identity must be a nonempty, trimmed string")
    if len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid identity")
    return value


def load(path):
    with Path(path).open("rb") as stream:
        data = tomllib.load(stream)
    keys(data, ("drives", "intervals", "output", "discovery_interval"))
    intervals = dict(DEFAULTS)
    supplied = data.get("intervals", {})
    keys(supplied, DEFAULTS)
    for name, value in supplied.items():
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 1 <= value <= 86400
        ):
            raise ValueError(f"{name} interval must be 1..86400 seconds")
        intervals[name] = value
    discovery_interval = data.get("discovery_interval", 60)
    if (
        type(discovery_interval) not in (int, float)
        or not math.isfinite(discovery_interval)
        or not 1 <= discovery_interval <= 86400
    ):
        raise ValueError("discovery_interval must be 1..86400 seconds")
    rows = data.get("drives")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError("configure 1..32 drives")
    drives, claims, bays = [], set(), set()
    for row in rows:
        keys(row, ("serial", "bay", "identity_aliases"))
        serial = identity(row.get("serial"))
        bay = row.get("bay")
        if "bay" in row and (type(bay) is not int or bay < 1):
            raise ValueError("bay must be a positive integer")
        aliases = row.get("identity_aliases", [])
        if not isinstance(aliases, list):
            raise ValueError("identity_aliases must be an array")
        aliases = tuple(identity(a) for a in aliases)
        if len(set((serial, *aliases))) != 1 + len(aliases) or claims.intersection(
            (serial, *aliases)
        ):
            raise ValueError("duplicate drive identity or alias")
        if bay is not None and bay in bays:
            raise ValueError("duplicate bay")
        claims.update((serial, *aliases))
        bays.add(bay)
        drives.append(Drive(serial, bay, aliases))
    output = data.get("output", {})
    keys(output, ("path",))
    path = output.get("path", "/run/disk-monitor/state.json")
    if (
        not isinstance(path, str)
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path)
        or ".." in Path(path).parts
    ):
        raise ValueError("output.path must be an absolute safe path")
    return Config(tuple(drives), intervals, Path(path), discovery_interval)
