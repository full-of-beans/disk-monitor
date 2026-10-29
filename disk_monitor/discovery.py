"""Read kernel and cached udev identity only. Never ask firmware for identity."""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Device:
    name: str
    identities: frozenset[str]
    generation: tuple[str, str, str]
    by_id: tuple[str, ...] = ()

    @property
    def path(self):
        return "/dev/" + self.name


def discover(
    sys_block=Path("/sys/block"),
    udev=Path("/run/udev/data"),
    by_id=Path("/dev/disk/by-id"),
):
    devices = []
    for entry in sorted(sys_block.iterdir()):
        # Initial release supports whole SCSI/SATA HDD devices, including USB SAT.
        if not re.fullmatch(r"sd[a-z]+", entry.name):
            continue
        try:
            location = str(entry.resolve())
            dev = (entry / "dev").read_text().strip()
            seq = (entry / "diskseq").read_text().strip()
            if not re.fullmatch(r"\d+:\d+", dev) or not seq.isdecimal():
                raise OSError("invalid required device metadata")
            identities = set()
            try:
                serial = (entry / "device/serial").read_text().strip()
                if serial:
                    identities.add(serial)
            except FileNotFoundError:
                pass
            try:
                for line in (udev / ("b" + dev)).read_text().splitlines():
                    for key in (
                        "E:ID_SERIAL_SHORT=",
                        "E:ID_SCSI_SERIAL=",
                        "E:ID_SERIAL=",
                    ):
                        if line.startswith(key) and line[len(key) :]:
                            identities.add(line[len(key) :])
            except FileNotFoundError:
                pass
            links = []
            for link in by_id.glob("*"):
                if re.search(r"-part\d+$", link.name):
                    continue
                if link.is_symlink() and link.resolve() == Path("/dev") / entry.name:
                    links.append(link.name)
                    # Exact full by-id basename is an opt-in alias, never substring matching.
                    identities.add(link.name)
            if (
                (entry / "diskseq").read_text().strip() != seq
                or (entry / "dev").read_text().strip() != dev
                or str(entry.resolve()) != location
            ):
                raise OSError("device changed during discovery")
            devices.append(
                Device(
                    entry.name,
                    frozenset(identities),
                    (location, dev, seq),
                    tuple(sorted(links)),
                )
            )
        except (OSError, UnicodeError) as error:
            # Incomplete enumeration cannot exclude an ambiguous identity.
            raise OSError(f"incomplete device metadata: {entry.name}") from error
    return devices


def resolve(drives, devices):
    matches = {
        d.serial: [
            v
            for v in devices
            if v.identities.intersection((d.serial, *d.identity_aliases))
        ]
        for d in drives
    }
    result = {}
    for drive in drives:
        found = matches[drive.serial]
        if not found:
            result[drive.serial] = ("missing", None)
        elif (
            len(found) != 1
            or sum(found[0] in values for values in matches.values()) != 1
        ):
            result[drive.serial] = ("ambiguous", None)
        else:
            result[drive.serial] = ("present", found[0])
    return result


def same_generation(device, sys_block=Path("/sys/block")):
    """Cheap kernel-only guard around counter reads."""
    entry = sys_block / device.name
    try:
        return device.generation == (
            str(entry.resolve()),
            (entry / "dev").read_text().strip(),
            (entry / "diskseq").read_text().strip(),
        )
    except (OSError, UnicodeError):
        return False
