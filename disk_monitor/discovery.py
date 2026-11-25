"""Resolve configured identities using cached Linux metadata."""

import errno
import os
import re
import stat
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

SYS_BLOCK = Path("/sys/block")


@dataclass(frozen=True)
class Generation:
    sysfs_path: str
    major_minor: str
    diskseq: int | None
    sysfs_inode: int


@dataclass(frozen=True)
class Device:
    name: str
    identities: frozenset[str]
    generation: Generation
    transport: str | None
    by_id: tuple[str, ...] = ()
    ata_serial: str | None = None

    @property
    def path(self):
        return "/dev/" + self.name


def read_optional(path):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def generation(entry):
    location = entry.resolve(strict=True)
    info = location.stat()
    dev = (location / "dev").read_text().strip()
    seq = read_optional(location / "diskseq")
    if (
        not re.fullmatch(r"[0-9]+:[0-9]+", dev)
        or (seq and not seq.isascii())
        or (seq and not seq.isdecimal())
    ):
        raise ValueError("invalid block device generation")
    return Generation(str(location), dev, int(seq) if seq else None, info.st_ino)


def reverse_aliases(by_id, dev_dir):
    aliases = defaultdict(list)
    try:
        links = list(by_id.iterdir())
    except FileNotFoundError:
        return aliases
    for link in links:
        if re.search(r"-part[0-9]+$", link.name) or not link.is_symlink():
            continue
        try:
            path = link.resolve(strict=True)
        except RuntimeError:  # pathlib before 3.13 reports symlink loops this way.
            continue
        except OSError as error:
            if error.errno not in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                raise
            continue
        if path.parent == dev_dir and re.fullmatch(r"sd[a-z]+", path.name):
            aliases[path.name].append(link.name)
    return aliases


def discover(
    sys_block=SYS_BLOCK,
    udev=Path("/run/udev/data"),
    by_id=Path("/dev/disk/by-id"),
    dev_dir=Path("/dev"),
):
    entries = {}
    for entry in sorted(sys_block.iterdir()):
        if re.fullmatch(r"sd[a-z]+", entry.name):
            try:
                entries[entry] = generation(entry)
            except (OSError, ValueError, RuntimeError) as error:
                raise OSError(
                    f"incomplete device generation: {entry.name}: {error}"
                ) from error
    aliases = reverse_aliases(by_id, dev_dir)
    devices = []
    for entry, current in entries.items():
        try:
            rotational = (entry / "queue/rotational").read_text().strip()
            if rotational not in {"0", "1"}:
                raise ValueError("invalid rotational flag")
            if rotational == "1" and (entry / "device/type").read_text().strip() == "0":
                device = read_identity(
                    entry, current, udev, aliases.get(entry.name, ())
                )
                if device:
                    devices.append(device)
            if generation(entry) != current:
                raise ValueError("device changed during discovery")
        except (OSError, ValueError, RuntimeError) as error:
            # Incomplete enumeration cannot rule out an ambiguous match.
            raise OSError(
                f"incomplete device metadata: {entry.name}: {error}"
            ) from error
    return devices


def read_identity(entry, current, udev, aliases):
    parts = Path(current.sysfs_path).parts
    usb = any(re.fullmatch(r"usb[0-9]+", part) for part in parts)
    native = (
        not usb
        and any(re.fullmatch(r"ata[0-9]+", part) for part in parts)
        and read_optional(entry / "device/vendor") == "ATA"
    )
    if not (native or usb):
        return None
    properties = dict(
        line[2:].split("=", 1)
        for line in read_optional(udev / ("b" + current.major_minor)).splitlines()
        if line.startswith("E:") and "=" in line
    )
    if (
        current.diskseq is not None
        and properties.get("DISKSEQ")
        and properties["DISKSEQ"] != str(current.diskseq)
    ):
        raise ValueError("udev has a different disk generation")
    transport = "ata" if native else "sat" if properties.get("ID_ATA") == "1" else None
    sys_serial = read_optional(entry / "device/serial")
    serial = properties.get("ID_SERIAL_SHORT") or (sys_serial if native else None)
    identities = {
        value
        for value in (
            *aliases,
            sys_serial,
            serial,
            properties.get("ID_SCSI_SERIAL"),
            properties.get("ID_SERIAL"),
        )
        if value
    }
    return Device(
        entry.name,
        frozenset(identities),
        current,
        transport,
        tuple(sorted(aliases)),
        serial,
    )


def resolve(drives, devices):
    index = defaultdict(list)
    serials = defaultdict(list)
    for device in devices:
        if device.ata_serial:
            serials[device.ata_serial].append(device)
        for identity in device.identities:
            index[identity].append(device)
    matches = {
        drive.serial: set(serials[drive.serial]).union(
            device for alias in drive.identity_aliases for device in index[alias]
        )
        for drive in drives
    }
    claims = Counter(device for found in matches.values() for device in found)
    result = {}
    for serial, found in matches.items():
        if not found:
            result[serial] = ("missing", None)
        elif len(found) != 1 or claims[next(iter(found))] != 1:
            result[serial] = ("ambiguous", None)
        else:
            device = next(iter(found))
            result[serial] = ("present" if device.transport else "unknown", device)
    return result


def same_generation(device, sys_block=SYS_BLOCK):
    try:
        return generation(sys_block / device.name) == device.generation
    except (OSError, ValueError, RuntimeError):
        return False


def same_block_node(device):
    try:
        info = os.stat(device.path, follow_symlinks=False)
        return stat.S_ISBLK(info.st_mode) and (
            f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
            == device.generation.major_minor
        )
    except OSError:
        return False
