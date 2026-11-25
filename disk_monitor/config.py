"""Strict TOML configuration for explicitly enrolled HDDs."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

RUNTIME = Path("/run/disk-monitor")


@dataclass(frozen=True)
class Drive:
    serial: str
    identity_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hook:
    name: str
    command: tuple[str, ...]
    timeout: float = 5


@dataclass(frozen=True)
class Config:
    drives: tuple[Drive, ...]
    interval: float = 60
    output: Path = RUNTIME / "state.json"
    counters: Path = RUNTIME / "counters.json"
    post_hooks: tuple[Hook, ...] = ()


def keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"expected table with keys: {', '.join(allowed)}")


def identity(value):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("identity must be a nonempty, trimmed string without controls")
    return value


def seconds(value, name, minimum, maximum):
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be {minimum}..{maximum} seconds")
    return value


def output_path(value, name):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
        or value.startswith("//")
        or value.endswith("/")
        or ".." in Path(value).parts
        or Path(value) == Path("/")
    ):
        raise ValueError(f"output.{name} must be an absolute file path")
    return Path(value)


def load(path):
    with Path(path).open("rb") as stream:
        data = tomllib.load(stream)
    keys(data, ("interval", "drives", "output", "post_hooks"))
    interval = seconds(data.get("interval", 60), "interval", 1, 86400)
    rows = data.get("drives")
    if not isinstance(rows, list) or not rows:
        raise ValueError("configure at least one drive serial")
    drives, claims = [], set()
    for row in rows:
        keys(row, ("serial", "identity_aliases"))
        serial = identity(row.get("serial"))
        aliases = row.get("identity_aliases", [])
        if not isinstance(aliases, list):
            raise ValueError("identity_aliases must be an array")
        aliases = tuple(identity(alias) for alias in aliases)
        identities = {serial, *aliases}
        if len(identities) != 1 + len(aliases) or claims & identities:
            raise ValueError("duplicate drive identity or alias")
        claims.update(identities)
        drives.append(Drive(serial, aliases))

    output = data.get("output", {})
    keys(output, ("path", "counters_path"))
    state = output_path(output.get("path", str(Config.output)), "path")
    counters = output_path(
        output.get("counters_path", str(Config.counters)), "counters_path"
    )
    if (
        state == counters
        or state.name.endswith(".lock")
        or counters.name.endswith(".lock")
    ):
        raise ValueError("output files must be distinct and cannot end in .lock")

    hooks, names = [], set()
    post_hooks = data.get("post_hooks", [])
    if not isinstance(post_hooks, list):
        raise ValueError("post_hooks must be an array of tables")
    for row in post_hooks:
        keys(row, ("name", "command", "timeout"))
        name = row.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("hook name must contain 1..64 letters, digits, _ or -")
        if name in names:
            raise ValueError(f"duplicate hook name: {name}")
        names.add(name)
        command = row.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(arg, str) or "\0" in arg for arg in command)
            or not command[0].strip()
        ):
            raise ValueError(f"hook {name} command must be a nonempty argument array")
        timeout = seconds(row.get("timeout", 5), f"hook {name} timeout", 0.1, 300)
        hooks.append(Hook(name, tuple(command), timeout))
    return Config(tuple(drives), interval, state, counters, tuple(hooks))
