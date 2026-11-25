"""Best-effort delivery with one in-flight subprocess per configured hook."""

import logging
import os
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

LOG = logging.getLogger(__name__)


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class Hooks:
    def __init__(self, hooks):
        self.hooks = hooks
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(hooks)))
        self.pending = {}
        self.processes = {}
        self.lock = Lock()
        self.closed = False

    def dispatch(self, payload):
        for hook in self.hooks:
            prior = self.pending.get(hook.name)
            if prior is not None and not prior.done():
                LOG.warning("hook %s still running, skipping snapshot", hook.name)
                continue
            self.pending[hook.name] = self.pool.submit(self.execute, hook, payload)

    def execute(self, hook, payload):
        try:
            with self.lock:
                if self.closed:
                    return
            process = subprocess.Popen(
                hook.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            LOG.warning("hook %s could not start: %s", hook.name, error)
            return
        try:
            with self.lock:
                self.processes[hook.name] = process
                if self.closed:
                    kill_group(process)
            try:
                process.communicate(payload, timeout=hook.timeout)
            except subprocess.TimeoutExpired:
                LOG.warning(
                    "hook %s timed out after %s seconds", hook.name, hook.timeout
                )
            else:
                if process.returncode:
                    LOG.warning(
                        "hook %s exited with status %s", hook.name, process.returncode
                    )
        except OSError as error:
            LOG.warning("hook %s delivery failed: %s", hook.name, error)
        finally:
            with self.lock:
                kill_group(process)
                self.processes.pop(hook.name, None)
            process.stdin.close()
            process.wait()

    def close(self):
        with self.lock:
            self.closed = True
            for process in self.processes.values():
                kill_group(process)
        self.pool.shutdown(wait=True, cancel_futures=True)
