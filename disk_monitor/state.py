"""Atomic disposable state and consumer-side freshness."""

import copy
import json
import math
import os
import tempfile
import time

SCHEMA_VERSION = 2


def publish(path, document):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(document, stream, allow_nan=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def with_freshness(document, now=None):
    now = time.time() if now is None else now
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != SCHEMA_VERSION
    ):
        raise ValueError("unsupported state schema")
    if not number(document.get("published_at")) or not isinstance(
        document.get("disks"), dict
    ):
        raise ValueError("invalid state document")
    bays = set()
    for serial, row in document["disks"].items():
        if (
            not isinstance(row, dict)
            or row.get("serial") != serial
            or row.get("presence") not in {"present", "missing", "ambiguous", "unknown"}
        ):
            raise ValueError("invalid disk row")
        bay = row.get("bay")
        active_at = row.get("last_active_at")
        if (
            bay is not None
            and (type(bay) is not int or bay < 1 or bay in bays)
            or active_at is not None
            and not number(active_at)
        ):
            raise ValueError("invalid bay or activity timestamp")
        if bay is not None:
            bays.add(bay)
        observations = row.get("observations")
        if not isinstance(observations, dict) or set(observations) != {
            "io",
            "spindle",
            "temperature",
            "self_test",
        }:
            raise ValueError("invalid observations")
        for observation in observations.values():
            if (
                not isinstance(observation, dict)
                or not number(observation.get("max_age_seconds"))
                or observation["max_age_seconds"] <= 0
            ):
                raise ValueError("invalid observation")
            stamp = observation.get("observed_at")
            if stamp is not None and not number(stamp):
                raise ValueError("invalid observation timestamp")
    result = copy.deepcopy(document)
    for row in result["disks"].values():
        for observation in row["observations"].values():
            stamp = observation["observed_at"]
            observation["fresh"] = bool(
                row["presence"] == "present"
                and stamp is not None
                and observation.get("error") is None
                and 0 <= now - stamp <= observation["max_age_seconds"]
            )
    return result
