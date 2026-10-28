"""Memory-safe, fault-tolerant process pool shared by build_tiles.py and
build_lerc.py.

Country-scale builds run hundreds of thousands of tiles through a pool of
workers that each hold every source COG open. Worker memory grows over time
(GDAL per-dataset state), and an unbounded pool previously pushed the Docker
VM into OOM / swap death. This runner keeps memory under a ceiling and keeps
one bad tile from killing a multi-hour build:

  * worker count sized from the memory limit (cgroup or VM RAM), not CPUs;
  * workers are recycled every RECYCLE chunks (max_tasks_per_child), which
    bounds per-worker growth;
  * a watchdog stops submitting work above HIGH_WATER of the limit, and if
    pressure persists with nothing in flight it restarts the pool with one
    worker fewer;
  * an exception in one tile is caught in the worker and reported to the
    parent (z/x/y + message) through on_error; the build continues and the
    caller exits non-zero at the end. More than MAX_FAIL_FRAC failures in one
    run_pool call raise PoolAborted;
  * if a worker is killed (BrokenProcessPool), unfinished chunks are
    re-queued and the pool restarts with fewer workers. From the second kill
    on, re-queued chunks are split into single tiles that run one at a time
    (isolation); a tile that kills its worker twice while running alone is
    quarantined as failed. Tile writes are atomic and resumable, so re-running
    a chunk is safe.

Tunables (env): BUILD_WORKER_GB (2.0), BUILD_MEM_HIGH (0.75),
BUILD_RECYCLE_CHUNKS (100), BUILD_MAX_FAIL_FRAC (0.01).
"""

import multiprocessing as mp
import os
import sys
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool

GB = 1024**3
PER_WORKER_BYTES = int(float(os.environ.get("BUILD_WORKER_GB", "2.0")) * GB)
HIGH_WATER = float(os.environ.get("BUILD_MEM_HIGH", "0.75"))
RECYCLE = int(os.environ.get("BUILD_RECYCLE_CHUNKS", "100"))
MAX_FAIL_FRAC = float(os.environ.get("BUILD_MAX_FAIL_FRAC", "0.01"))
MIN_FAIL_ABORT = 5      # never abort on fewer failures than this
CHUNK = 64
MAX_RESTARTS = 10       # pool deaths outside isolation before giving up
ISOLATED_KILLS = 2      # kills while running alone -> tile quarantined
MAX_LOGGED_ERRORS = 50


class PoolAborted(RuntimeError):
    """Too many tiles failed; the caller should stop the build."""


def _read_cgroup_int(path):
    try:
        with open(path) as f:
            v = f.read().strip()
        return None if v == "max" else int(v)
    except (OSError, ValueError):
        return None


def cgroup_used(stat_path="/sys/fs/cgroup/memory.stat"):
    """Non-reclaimable cgroup memory: anon + kernel. Page cache is left out
    (reclaimable); kernel memory (slab, stacks, page tables) is not. Uses the
    'kernel' line when present (Linux >= 5.18), else kernel_stack + slab +
    pagetables. None when memory.stat is unreadable."""
    fields = {}
    try:
        with open(stat_path) as f:
            for line in f:
                k, _, v = line.partition(" ")
                try:
                    fields[k] = int(v)
                except ValueError:
                    pass
    except OSError:
        return None
    if "anon" not in fields:
        return None
    kernel = fields.get("kernel")
    if kernel is None:
        kernel = sum(fields.get(k, 0) for k in ("kernel_stack", "slab", "pagetables"))
    return fields["anon"] + kernel


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
        used = cgroup_used()
        if used is not None:
            candidates.append((used, cg_max))
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


def describe_task(task) -> str:
    """'z/x/y' for (z, x, y, ...) tile tasks, else a short repr."""
    if isinstance(task, tuple) and len(task) >= 3 and all(
            isinstance(v, int) for v in task[:3]):
        return f"{task[0]}/{task[1]}/{task[2]}"
    r = repr(task)
    return r if len(r) <= 80 else r[:77] + "..."


def _run_chunk(job, chunk):
    """Runs in the worker: one tile's exception must not lose the chunk."""
    out = []
    for t in chunk:
        try:
            out.append(("ok", job(t)))
        except Exception as e:  # noqa: BLE001
            tb = traceback.extract_tb(e.__traceback__)
            where = f" at {os.path.basename(tb[-1].filename)}:{tb[-1].lineno}" if tb else ""
            out.append(("error", describe_task(t), f"{type(e).__name__}: {e}{where}"))
    return out


def run_pool(job, tasks, workers, initializer, initargs, on_result, label,
             on_error=None):
    """Run job(task) for every task in a memory-safe pool, calling
    on_result(result) in the parent for each success and
    on_error(task_desc, message) for each failed tile (exception in the job,
    or quarantined after killing its worker). Returns the worker count still
    in use, so the caller can carry a reduced count into the next zoom.
    Raises PoolAborted when more than MAX_FAIL_FRAC of the tasks fail."""
    pending = [list(tasks[i:i + CHUNK]) for i in range(0, len(tasks), CHUNK)]
    pending.reverse()  # pop() from the end keeps roughly row order
    isolate = []       # single tiles to run alone after repeated pool deaths
    kills = {}         # task desc -> deaths while running alone
    total, done, next_report = len(tasks), 0, 4096
    restarts = 0
    failed = 0
    fail_limit = max(MIN_FAIL_ABORT, int(total * MAX_FAIL_FRAC))

    def fail(desc, msg):
        nonlocal failed
        failed += 1
        if failed <= MAX_LOGGED_ERRORS:
            print(f"  {label}: TILE FAILED {desc}: {msg}")
        elif failed == MAX_LOGGED_ERRORS + 1:
            print(f"  {label}: more tile failures; only counting from now on")
        sys.stdout.flush()
        if on_error is not None:
            on_error(desc, msg)
        if failed > fail_limit:
            raise PoolAborted(f"{failed} of {total} tiles failed (limit {fail_limit}, "
                              f"{MAX_FAIL_FRAC:.0%}); last: {desc}: {msg}")

    while pending or isolate:
        pool = ProcessPoolExecutor(
            max_workers=workers, mp_context=mp.get_context("spawn"),
            initializer=initializer, initargs=initargs,
            max_tasks_per_child=RECYCLE,
        )
        inflight = {}
        iso_chunk = None   # the single tile in flight in isolation mode
        shrink = False
        throttled_idle = 0
        try:
            while pending or isolate or inflight:
                if isolate:
                    # isolation mode: exactly one suspect tile in flight
                    if not inflight:
                        iso_chunk = chunk = isolate.pop()
                        inflight[pool.submit(_run_chunk, job, chunk)] = chunk
                else:
                    while pending and len(inflight) < workers * 2:
                        frac = mem_frac()
                        # Over the high-water mark: stop feeding the pool. With
                        # a single worker and nothing in flight, still run one
                        # chunk at a time so the build never stalls.
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
                        if r[0] == "ok":
                            on_result(r[1])
                        else:
                            fail(r[1], r[2])
                    done += len(chunk)
                    if done >= next_report:
                        frac = mem_frac()
                        mem = f", mem {frac:.0%}" if frac is not None else ""
                        print(f"  {label} progress {done}/{total} workers={workers}{mem}")
                        sys.stdout.flush()
                        next_report += 4096
        except BrokenProcessPool:
            lost = list(inflight.values())
            inflight.clear()
            if iso_chunk is not None and len(lost) == 1 and lost[0] is iso_chunk:
                # a single tile running alone killed its worker
                desc = describe_task(lost[0][0])
                kills[desc] = kills.get(desc, 0) + 1
                if kills[desc] >= ISOLATED_KILLS:
                    fail(desc, f"worker process died {kills[desc]} times while "
                               f"building this tile alone (quarantined)")
                    done += 1
                else:
                    isolate.append(lost[0])
                print(f"  {label}: worker died on tile {desc} "
                      f"(isolated run {kills[desc]}/{ISOLATED_KILLS})")
            else:
                restarts += 1
                if restarts > MAX_RESTARTS:
                    raise
                if restarts >= 2:
                    # repeated deaths: find the culprit by running the
                    # unfinished tiles one at a time
                    for ch in lost:
                        isolate.extend([t] for t in ch)
                    how = f"isolating {sum(len(c) for c in lost)} unfinished tile(s)"
                else:
                    pending.extend(lost)
                    how = "re-queued unfinished tiles"
                workers = max(1, workers * 2 // 3)
                print(f"  {label}: a worker was killed (likely out of memory); "
                      f"{how}, continuing with {workers} workers")
            sys.stdout.flush()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if shrink:
            workers -= 1
            frac = mem_frac()
            mem = f"{frac:.0%}" if frac is not None else "?"
            print(f"  {label}: memory high ({mem}); restarting pool with "
                  f"{workers} workers")
            sys.stdout.flush()
    return workers

