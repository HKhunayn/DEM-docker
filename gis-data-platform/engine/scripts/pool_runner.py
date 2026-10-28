"""Memory-safe process pool shared by build_tiles.py and build_lerc.py.

Country-scale builds run hundreds of thousands of tiles through a pool of
workers that each hold every source COG open. Worker memory grows over time
(GDAL per-dataset state), and an unbounded pool previously pushed the Docker
VM into OOM / swap death. This runner keeps memory under a ceiling:

  * worker count sized from the memory limit (cgroup or VM RAM), not CPUs;
  * workers are recycled every RECYCLE chunks (max_tasks_per_child), which
    bounds per-worker growth;
  * a watchdog stops submitting work above HIGH_WATER of the limit, and if
    pressure persists with nothing in flight it restarts the pool with one
    worker fewer;
  * if a worker is still killed (BrokenProcessPool), unfinished chunks are
    re-queued and the pool restarts with fewer workers. Tile writes are
    atomic and resumable, so re-running a chunk is safe.

Tunables (env): BUILD_WORKER_GB (2.0), BUILD_MEM_HIGH (0.75),
BUILD_RECYCLE_CHUNKS (100).
"""

import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool

GB = 1024**3
PER_WORKER_BYTES = int(float(os.environ.get("BUILD_WORKER_GB", "2.0")) * GB)
HIGH_WATER = float(os.environ.get("BUILD_MEM_HIGH", "0.75"))
RECYCLE = int(os.environ.get("BUILD_RECYCLE_CHUNKS", "100"))
CHUNK = 64
MAX_RESTARTS = 10


def _read_cgroup_int(path):
    try:
        with open(path) as f:
            v = f.read().strip()
        return None if v == "max" else int(v)
    except (OSError, ValueError):
        return None


def mem_candidates():
    """[(used_bytes, limit_bytes), ...] for the VM's RAM and, when set, this
    container's cgroup memory limit. Empty when unknown (non-Linux)."""
    total = avail = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                if k == "MemTotal":
                    total = int(v.split()[0]) * 1024
                elif k == "MemAvailable":
                    avail = int(v.split()[0]) * 1024
    except OSError:
        return []
    if total is None or avail is None:
        return []
    candidates = [(total - avail, total)]
    cg_max = _read_cgroup_int("/sys/fs/cgroup/memory.max")
    if cg_max:
        anon = None  # page cache is reclaimable; count anonymous memory only
        try:
            with open("/sys/fs/cgroup/memory.stat") as f:
                for line in f:
                    if line.startswith("anon "):
                        anon = int(line.split()[1])
                        break
        except OSError:
            pass
        if anon is not None:
            candidates.append((anon, cg_max))
    return candidates


def mem_status():
    """(used, limit) of whichever limit is most loaded, or None."""
    c = mem_candidates()
    return max(c, key=lambda u: u[0] / u[1]) if c else None


def mem_frac():
    st = mem_status()
    return None if st is None else st[0] / st[1]


def default_workers() -> int:
    """Workers that fit under HIGH_WATER of the tightest limit (VM or cgroup)."""
    cpus = os.cpu_count() or 4
    c = mem_candidates()
    if not c:
        return cpus
    budget = min(limit * HIGH_WATER - used for used, limit in c)
    return max(1, min(cpus, int(budget // PER_WORKER_BYTES)))


def describe_limits() -> str:
    c = mem_candidates()
    if not c:
        return "memory limit unknown"
    used, limit = min(c, key=lambda u: u[1] * HIGH_WATER - u[0])
    return (f"memory {used / GB:.1f}/{limit / GB:.1f} GB, pause above "
            f"{HIGH_WATER:.0%}, recycle workers every {RECYCLE * CHUNK} tiles")


def _run_chunk(job, chunk):
    return [job(t) for t in chunk]


def run_pool(job, tasks, workers, initializer, initargs, on_result, label):
    """Run job(task) for every task in a memory-safe pool, calling
    on_result(result) in the parent. Returns the worker count still in use,
    so the caller can carry a reduced count into the next zoom level."""
    pending = [tasks[i:i + CHUNK] for i in range(0, len(tasks), CHUNK)]
    pending.reverse()  # pop() from the end keeps roughly row order
    total, done, next_report = len(tasks), 0, 4096
    restarts = 0

    while pending:
        pool = ProcessPoolExecutor(
            max_workers=workers, mp_context=mp.get_context("spawn"),
            initializer=initializer, initargs=initargs,
            max_tasks_per_child=RECYCLE,
        )
        inflight = {}
        shrink = False
        throttled_idle = 0
        try:
            while pending or inflight:
                while pending and len(inflight) < workers * 2:
                    frac = mem_frac()
                    # Over the high-water mark: stop feeding the pool. With a
                    # single worker and nothing in flight, still run one chunk
                    # at a time so the build never stalls.
                    if frac is not None and frac > HIGH_WATER and (inflight or workers > 1):
                        break
                    chunk = pending.pop()
                    inflight[pool.submit(_run_chunk, job, chunk)] = chunk
                if not inflight:
                    # throttled with nothing running: idle workers still hold memory
                    throttled_idle += 1
                    if throttled_idle >= 3 and workers > 1:
                        shrink = True
                        break
                    time.sleep(2)
                    continue
                throttled_idle = 0
                finished, _ = wait(inflight, timeout=5, return_when=FIRST_COMPLETED)
                for fut in finished:
                    results = fut.result()  # raises BrokenProcessPool if a worker died
                    chunk = inflight.pop(fut)
                    for r in results:
                        on_result(r)
                    done += len(chunk)
                    if done >= next_report:
                        frac = mem_frac()
                        mem = f", mem {frac:.0%}" if frac is not None else ""
                        print(f"  {label} progress {done}/{total} workers={workers}{mem}")
                        sys.stdout.flush()
                        next_report += 4096
        except BrokenProcessPool:
            restarts += 1
            if restarts > MAX_RESTARTS:
                raise
            pending.extend(inflight.values())
            inflight.clear()
            workers = max(1, workers * 2 // 3)
            print(f"  {label}: a worker was killed (likely out of memory); "
                  f"re-queued unfinished tiles, continuing with {workers} workers")
            sys.stdout.flush()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if shrink:
            workers -= 1
            frac = mem_frac()
            print(f"  {label}: memory high ({frac:.0%}); restarting pool with "
                  f"{workers} workers")
            sys.stdout.flush()
    return workers
