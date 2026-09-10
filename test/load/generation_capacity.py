"""Load test for the image-generation worker pool (the real launch bottleneck).

Unlike the k6 /health tests, this does NOT go over HTTP. It drives the REAL
`submit_image_task` from src.core.task_executor with a fake generation function
(a sleep, standing in for a slow Replicate call). No network, no LINE, no cost.

It answers the question k6 cannot: when many users ask to generate at once,
which ones run, which ones queue, and which one gets the "busy" rejection.

The pool is bounded: IMAGE_WORKERS run concurrently, IMAGE_QUEUE_LIMIT wait,
and anything beyond that is rejected (submit returns False -> user sees busy).

Usage:
    python3 test/load/generation_capacity.py
    USERS=30 GEN_SECONDS=8 python3 test/load/generation_capacity.py
    IMAGE_WORKERS=8 IMAGE_QUEUE_LIMIT=16 USERS=40 python3 test/load/generation_capacity.py
"""

import os
import sys
import time
import threading
from statistics import median

# Make src importable when run from repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# NOTE: task_executor reads IMAGE_WORKERS / IMAGE_QUEUE_LIMIT at import time,
# so any override must be set in the environment before this import.
from src.core import task_executor  # noqa: E402
from src.core.task_executor import submit_image_task  # noqa: E402

USERS = int(os.getenv("USERS", "20"))          # simultaneous generation requests
GEN_SECONDS = float(os.getenv("GEN_SECONDS", "6"))  # fake Replicate duration per job
MB_PER_JOB = float(os.getenv("MB_PER_JOB", "5"))    # image/result bytes held in RAM per job

# Current-RSS reader. psutil is accurate & cross-platform; without it we shell
# out to `ps -o rss=` which returns *current* RSS in KB on both macOS and Linux.
try:
    import psutil  # type: ignore
    _proc = psutil.Process()

    def rss_mb():
        return _proc.memory_info().rss / (1024 * 1024)
    _RSS_MODE = "psutil (current)"
except Exception:
    import subprocess

    def rss_mb():
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())])
        return int(out.strip()) / 1024  # KB -> MB
    _RSS_MODE = "ps -o rss (current)"

# Shared state for observing real concurrency and timings.
_lock = threading.Lock()
_active = 0
_max_active = 0
_peak_rss = 0.0
_monitor_stop = threading.Event()
records = {}  # index -> dict(accepted, enqueue, start, finish)


def _monitor_rss():
    """Sample RSS while the test runs so we catch the peak."""
    global _peak_rss
    while not _monitor_stop.is_set():
        _peak_rss = max(_peak_rss, rss_mb())
        time.sleep(0.2)


def make_job(i, buf):
    """A fake generation job. `buf` is the input image bytes, allocated BEFORE
    submit and captured in this closure — exactly like the real code does with
    `run=lambda: self.run_model(image_bytes)`. So a queued job keeps holding its
    image while it waits, not just while it runs."""
    def _job():
        global _active, _max_active
        with _lock:
            _active += 1
            _max_active = max(_max_active, _active)
            records[i]["start"] = time.monotonic()
        time.sleep(GEN_SECONDS)
        with _lock:
            _active -= 1
            records[i]["finish"] = time.monotonic()
        del buf  # released when the generation finishes and the task is dropped
    return _job


def submit_one(i, t0):
    records[i] = {"enqueue": time.monotonic() - t0}
    # Download-then-submit: the image bytes exist before the task is queued, held
    # for the whole queue+run lifetime via the closure. Use random (incompressible)
    # bytes — real JPEG/MP4 is already compressed, and this defeats macOS memory
    # compression so the RSS number matches what a Linux box would actually hold.
    buf = bytearray(os.urandom(int(MB_PER_JOB * 1024 * 1024)))
    accepted = submit_image_task(make_job(i, buf))
    records[i]["accepted"] = accepted
    if not accepted:
        del buf  # busy path: bytes released right after the rejection reply


def main():
    workers = task_executor._MAX_WORKERS
    pending = task_executor._MAX_PENDING
    capacity = workers + pending

    print(f"pool: {workers} running + {pending} queued = {capacity} capacity")
    print(f"simulating {USERS} users at once, each job = {GEN_SECONDS}s, "
          f"{MB_PER_JOB}MB in RAM")
    print(f"RSS source: {_RSS_MODE}\n")

    # Baseline RSS before any job allocates, then start sampling for the peak.
    baseline_rss = rss_mb()
    monitor = threading.Thread(target=_monitor_rss, daemon=True)
    monitor.start()

    t0 = time.monotonic()
    # Fire all submissions near-simultaneously to mimic a community flood.
    threads = [threading.Thread(target=submit_one, args=(i, t0)) for i in range(USERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = [i for i in records if records[i]["accepted"]]
    busy = [i for i in records if not records[i]["accepted"]]
    first_busy = min(busy) + 1 if busy else None

    print(f"submitted : {USERS}")
    print(f"accepted  : {len(accepted)}  (ran or queued)")
    print(f"busy (rejected): {len(busy)}  -> these users get the busy message")
    if first_busy:
        print(f"first busy at request #: {first_busy}")

    # Wait for all accepted jobs to finish so we can measure queue drain.
    while True:
        with _lock:
            done = all("finish" in records[i] for i in accepted)
        if done:
            break
        time.sleep(0.1)

    _monitor_stop.set()
    total_wall = time.monotonic() - t0
    waits = sorted(records[i]["start"] - records[i]["enqueue"] for i in accepted)

    print(f"\nmax concurrent jobs actually running: {_max_active}  (should equal {workers})")
    print(f"queue wait for accepted jobs: "
          f"min={waits[0]:.1f}s  median={median(waits):.1f}s  max={waits[-1]:.1f}s")
    print(f"total time to drain everything: {total_wall:.1f}s")

    delta = _peak_rss - baseline_rss
    print(f"\nRAM: baseline={baseline_rss:.0f}MB  peak={_peak_rss:.0f}MB  "
          f"delta=+{delta:.0f}MB")
    if _max_active:
        print(f"     ~{delta / _max_active:.1f}MB per concurrent job "
              f"(theoretical: {MB_PER_JOB}MB)")

    # Show the wave structure: each queued batch starts ~GEN_SECONDS after the last.
    print("\nper-request (accepted): queue-wait -> when it started")
    for i in accepted:
        print(f"  #{i + 1:>2}: waited {records[i]['start'] - records[i]['enqueue']:>4.1f}s")


if __name__ == "__main__":
    main()
