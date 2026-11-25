"""Atomic JSON publication."""

import json
import os
import tempfile

SCHEMA_VERSION = 4


def encode(document):
    return (json.dumps(document, allow_nan=False, sort_keys=True) + "\n").encode()


def publish(path, payload):
    descriptor, name = tempfile.mkstemp(prefix=".disk-monitor-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(payload)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
