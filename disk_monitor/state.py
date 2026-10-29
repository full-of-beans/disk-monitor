"""Atomic disposable state and consumer-side freshness."""

import copy
import json
import math
import os
import tempfile
import time

SCHEMA_VERSION = 1


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
    for serial, row in document["disks"].items():
        if (
            not isinstance(row, dict)
            or row.get("serial") != serial
            or row.get("presence") not in {"present", "missing", "ambiguous", "unknown"}
        ):
            raise ValueError("invalid disk row")
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
    published = result["published_at"]
    result["service_fresh"] = 0 <= now - published <= 5
    for row in result["disks"].values():
        for observation in row["observations"].values():
            stamp = observation["observed_at"]
            observation["fresh"] = bool(
                result["service_fresh"]
                and row["presence"] == "present"
                and stamp is not None
                and observation.get("error") is None
                and 0 <= now - stamp <= observation["max_age_seconds"]
            )
    return result
