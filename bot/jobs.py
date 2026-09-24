"""Background worker for commands that take minutes (Steam downloads, updates).

One job at a time, by design: two people running /update and /addmap at once
would fight over the same server process.
"""

import os
import queue
import signal
import subprocess
import threading
import time


class Job:
    def __init__(self, name, who):
        self.name = name
        self.who = who
        self.started_at = time.time()

    def age(self):
        seconds = int(time.time() - self.started_at)
        return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m{seconds % 60:02d}s"


class JobRunner:
    """A single worker thread draining a queue, with a one-job-at-a-time lock."""

    def __init__(self, log=print):
        self.queue = queue.Queue()
        self.log = log
        self._lock = threading.Lock()
        self._current = None
        self._thread = None

    @property
    def current(self):
        with self._lock:
            return self._current

    def start(self):
        self._thread = threading.Thread(target=self._work, name="l4d2-jobs", daemon=True)
        self._thread.start()
        return self

    def submit(self, name, who, fn):
        """Queue a job. Returns (accepted, message)."""
        running = self.current
        if running:
            return False, f"Busy: {running.name} has been running for {running.age()}. Try again when it finishes."
        self.queue.put((Job(name, who), fn))
        return True, None

    def _work(self):
        while True:
            job, fn = self.queue.get()
            with self._lock:
                self._current = job
            try:
                fn()
            except Exception as e:
                self.log(f"job {job.name} failed: {e!r}")
            finally:
                with self._lock:
                    self._current = None
                self.queue.task_done()


def run(argv, timeout=120, on_output=None):
    """Run a command, streaming combined output. Returns (exit_code, text).

    stdin is /dev/null so anything that would prompt sees a non-tty and gives up
    rather than hanging the worker.
    """
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            errors="replace",
            start_new_session=True,  # own process group, so we can kill children too
        )
    except OSError as e:
        return 127, f"Could not run {argv[0]}: {e}"

    lines = []
    deadline = time.time() + timeout
    timed_out = False

    def reader():
        for line in process.stdout:
            lines.append(line)
            if on_output:
                try:
                    on_output(line.rstrip("\n"), "".join(lines))
                except Exception:
                    pass

    pump = threading.Thread(target=reader, daemon=True)
    pump.start()

    while pump.is_alive() and time.time() < deadline:
        pump.join(0.5)

    if pump.is_alive():
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        pump.join(5)

    process.wait()
    text = "".join(lines).strip()
    if timed_out:
        text += f"\n\n[timed out after {timeout}s and was killed]"
        return 124, text
    return process.returncode, text


class Throttle:
    """Rate-limit progress edits so we don't trip Telegram's own limits."""

    def __init__(self, interval=4.0):
        self.interval = interval
        self._last = 0.0

    def ready(self):
        now = time.time()
        if now - self._last >= self.interval:
            self._last = now
            return True
        return False
