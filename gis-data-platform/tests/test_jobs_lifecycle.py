"""Job runner restart safety: persistence, reconciliation, cancel, lock file, watchdog."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from .conftest import reload_platform

JOB = {"name": "local30m", "bbox": [6.6, 45.6, 7.2, 46.0], "provider": "copernicus", "minzoom": 0, "maxzoom": 13}
TERMINAL = ("done", "error", "cancelled", "interrupted")


def jobs_mod():
    return sys.modules["app.jobs"]


def wait_state(client, job_id, states=TERMINAL, timeout=20.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last["state"] in states:
            return last
        time.sleep(0.05)
    raise AssertionError(f"job did not reach {states}: {last}")


def wait_step(client, job_id, step, timeout=10.0):
    """Wait until the engine process of `step` is running and has printed its banner."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if any(line.startswith(f"stub {step}:") for line in job["log"]):
            return job
        time.sleep(0.05)
    raise AssertionError(f"step {step} never started")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # zombies count as dead
        return open(f"/proc/{pid}/stat").read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


posix_only = pytest.mark.skipif(os.name != "posix", reason="process-group semantics tested on POSIX")


# ---- persistence + restart -------------------------------------------------------

def test_job_record_and_log_are_persisted(client, tmp_path):
    started = client.post("/api/jobs", json=JOB).json()
    job = wait_state(client, started["id"])
    assert job["state"] == "done"
    jobs_dir = tmp_path / "data" / "jobs"
    record = json.loads((jobs_dir / f"{job['id']}.json").read_text(encoding="utf-8"))
    assert record["state"] == "done"
    assert "log" not in record
    assert record["result"]["tilesBuilt"] == 42
    log_lines = (jobs_dir / f"{job['id']}.log").read_text(encoding="utf-8").splitlines()
    assert any("WROTE 42 tiles" in line for line in log_lines)
    assert job["log_total"] == len(log_lines)
    assert not list(jobs_dir.glob("*.tmp"))
    # lock file is released when the job ends
    assert not (tmp_path / "data" / ".job.lock").exists()


def test_restart_marks_running_job_and_building_area_interrupted(client, tmp_path):
    # Simulate a crash: a job record left 'running' + an area left 'building'.
    area = client.post("/api/areas", json={**{k: JOB[k] for k in ("name", "bbox", "provider", "minzoom", "maxzoom")}}).json()
    catalog = sys.modules["app.catalog"]
    catalog.update_area(area["id"], dataset={"name": "local30m", "state": "building"})
    job_id = uuid.uuid4().hex
    jobs_dir = tmp_path / "data" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": job_id, "kind": "elevation", "name": "local30m", "provider": "copernicus",
        "bbox": JOB["bbox"], "minzoom": 0, "maxzoom": 13, "areaId": area["id"],
        "state": "running", "step": "build",
        "steps": [{"name": "fetch", "state": "done", "returncode": 0},
                  {"name": "build", "state": "running", "returncode": None},
                  {"name": "verify", "state": "pending", "returncode": None}],
        "started": "2026-10-28T00:00:00+00:00", "finished": None, "result": None, "error": None,
        "_t0": time.time() - 100, "_finished_t": None, "_log_total": 3,
    }
    (jobs_dir / f"{job_id}.json").write_text(json.dumps(record), encoding="utf-8")
    (jobs_dir / f"{job_id}.log").write_text("".join(f"line {i}\n" for i in range(7000)), encoding="utf-8")

    reload_platform()  # == API restart: app.jobs re-imports and reconciles
    jobs = jobs_mod()
    job = jobs.get_job(job_id, tail=5)
    assert job["state"] == "interrupted"
    assert job["error"] == jobs.INTERRUPTED_MSG
    assert job["error"].startswith("interrupted by server restart")
    assert job["steps"][1]["state"] == "interrupted"
    assert job["log"] == [f"line {i}" for i in range(6995, 7000)]
    assert job["log_total"] == 7000
    assert job["elapsed"] > 0 and job["finished"]
    saved = json.loads((jobs_dir / f"{job_id}.json").read_text(encoding="utf-8"))
    assert saved["state"] == "interrupted"
    assert jobs.current_job()["id"] == job_id
    assert jobs._CURRENT_ID is None  # nothing blocks a new job
    area_now = catalog.find_area(area["id"])
    assert area_now["dataset"]["state"] == "interrupted"
    assert area_now["dataset"]["error"] == jobs.INTERRUPTED_MSG

    # starting the same area again works (resume)
    snap = jobs.start_job("local30m", JOB["bbox"], "copernicus", 0, 13)
    jobs._wait_threads()
    assert jobs.get_job(snap["id"])["state"] == "done"


def test_history_survives_reload_and_is_bounded(client):
    jobs = jobs_mod()
    jobs.HISTORY_LIMIT = 3  # reset by the next reload
    ids = []
    for i in range(4):
        ids.append(client.post("/api/jobs", json={**JOB, "name": f"a{i}", "saveArea": False}).json()["id"])
        wait_state(client, ids[-1])
    listing = client.get("/api/jobs").json()
    assert [j["id"] for j in listing["jobs"]] == ids[::-1][:3]
    assert all("log" not in j and j["log_total"] > 0 for j in listing["jobs"])
    jobs_dir = jobs.JOBS_ROOT
    assert not (jobs_dir / f"{ids[0]}.json").exists()  # pruned with the history
    reload_platform()
    reloaded = jobs_mod()
    assert [j["id"] for j in reloaded.list_jobs()] == ids[::-1][:3]
    assert reloaded.get_job(ids[-1])["state"] == "done"


# ---- tail / log_total ---------------------------------------------------------------

def test_tail_query_limits_log_lines(client):
    started = client.post("/api/jobs", json=JOB).json()
    job = wait_state(client, started["id"])
    total = job["log_total"]
    assert total > 5 and len(job["log"]) == total  # default tail 200 > total
    two = client.get(f"/api/jobs/{started['id']}?tail=2").json()
    assert two["log"] == job["log"][-2:] and two["log_total"] == total
    assert client.get(f"/api/jobs/{started['id']}?tail=0").json()["log"] == []
    assert len(client.get(f"/api/jobs/{started['id']}?tail=999999").json()["log"]) == total
    current = client.get("/api/jobs/current?tail=1").json()
    assert current["log"] == job["log"][-1:] and current["log_total"] == total


def test_in_memory_log_is_bounded_but_total_counts_everything(client):
    jobs = jobs_mod()
    job = jobs._new_job("elevation", "x", "copernicus", [0, 0, 1, 1], 0, 1, None, jobs.STEPS)
    for i in range(jobs.LOG_TAIL_LIMIT + 50):
        jobs._append_log(job, f"l{i}")
    snap = jobs._snapshot(job, tail=10_000)
    assert len(snap["log"]) == jobs.LOG_TAIL_LIMIT
    assert snap["log_total"] == jobs.LOG_TAIL_LIMIT + 50
    assert snap["log"][-1] == f"l{jobs.LOG_TAIL_LIMIT + 49}"


def test_current_job_404_when_none(client):
    assert client.get("/api/jobs/current").status_code == 404


# ---- try/finally ----------------------------------------------------------------------

def test_crash_in_job_thread_clears_current_and_sets_terminal_state(client, monkeypatch):
    jobs = jobs_mod()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(jobs, "build_command", boom)
    started = client.post("/api/jobs", json=JOB).json()
    job = wait_state(client, started["id"])
    assert job["state"] == "error" and "simulated crash" in job["error"]
    assert jobs._CURRENT_ID is None
    area = client.get(f"/api/areas/{started['areaId']}").json()
    assert area["dataset"]["state"] == "error"


def test_catalog_failure_after_job_does_not_wedge_runner(client, monkeypatch):
    jobs = jobs_mod()
    catalog = sys.modules["app.catalog"]

    def broken_update(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(catalog, "update_area", broken_update)
    first = client.post("/api/jobs", json=JOB).json()
    assert wait_state(client, first["id"])["state"] == "done"
    jobs._wait_threads()
    assert jobs._CURRENT_ID is None
    second = client.post("/api/jobs", json=JOB)
    assert second.status_code == 202
    wait_state(client, second.json()["id"])


# ---- cancel -----------------------------------------------------------------------------

@posix_only
def test_cancel_terminates_engine_process_group(client, monkeypatch, tmp_path):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("STUB_SLEEP", "60")
    monkeypatch.setenv("STUB_SPAWN_CHILD", "1")
    monkeypatch.setenv("STUB_CHILD_PIDFILE", str(pidfile))
    started = client.post("/api/jobs", json=JOB).json()
    wait_step(client, started["id"], "build")
    deadline = time.time() + 10
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    child = int(pidfile.read_text())
    assert pid_alive(child)
    lock = json.loads((tmp_path / "data" / ".job.lock").read_text())
    assert lock["jobId"] == started["id"] and pid_alive(lock["pid"])

    t0 = time.time()
    response = client.post(f"/api/jobs/{started['id']}/cancel")
    assert response.status_code == 200
    job = response.json()
    assert time.time() - t0 < 15
    assert job["state"] == "cancelled"
    assert job["error"] == "cancelled by user"
    assert job["steps"][1]["state"] == "cancelled"
    assert job["steps"][2]["state"] == "pending"
    assert not pid_alive(lock["pid"])
    deadline = time.time() + 5
    while pid_alive(child) and time.time() < deadline:
        time.sleep(0.05)
    assert not pid_alive(child), "grandchild in the engine's process group survived cancel"
    assert not (tmp_path / "data" / ".job.lock").exists()
    assert jobs_mod()._CURRENT_ID is None
    area = client.get(f"/api/areas/{started['areaId']}").json()
    assert area["dataset"]["state"] == "cancelled"

    # 409 for a job that is no longer running, 404 for an unknown id
    assert client.post(f"/api/jobs/{started['id']}/cancel").status_code == 409
    assert client.post(f"/api/jobs/{uuid.uuid4().hex}/cancel").status_code == 404
    # the runner is free again
    monkeypatch.delenv("STUB_SLEEP")
    again = client.post("/api/jobs", json=JOB)
    assert again.status_code == 202
    assert wait_state(client, again.json()["id"])["state"] == "done"


@posix_only
def test_cancel_escalates_to_sigkill(client, monkeypatch, tmp_path):
    jobs = jobs_mod()
    monkeypatch.setattr(jobs, "TERM_GRACE_S", 0.5)
    engine = jobs.ENGINE_ROOT / "terrainhub.py"
    engine.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('stub ignoring SIGTERM', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    started = client.post("/api/jobs", json=JOB).json()
    deadline = time.time() + 10
    while client.get(f"/api/jobs/{started['id']}").json()["log_total"] < 1 and time.time() < deadline:
        time.sleep(0.05)
    t0 = time.time()
    job = client.post(f"/api/jobs/{started['id']}/cancel").json()
    assert job["state"] == "cancelled"
    assert time.time() - t0 < 10
    assert job["steps"][0]["returncode"] == -9


def test_shutdown_marks_running_job_interrupted(client, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "60")
    started = client.post("/api/jobs", json=JOB).json()
    wait_step(client, started["id"], "build")
    jobs = jobs_mod()
    jobs.shutdown(grace=2.0)
    job = jobs.get_job(started["id"])
    assert job["state"] == "interrupted"
    assert job["error"] == jobs.INTERRUPTED_MSG
    saved = json.loads((jobs.JOBS_ROOT / f"{started['id']}.json").read_text(encoding="utf-8"))
    assert saved["state"] == "interrupted"
    assert jobs._CURRENT_ID is None


# ---- lock file ----------------------------------------------------------------------------

@posix_only
def test_live_orphan_engine_in_lock_file_blocks_new_job(client, tmp_path):
    jobs = jobs_mod()
    orphan = subprocess.Popen(
        [sys.executable, str(jobs.ENGINE_ROOT / "terrainhub.py"), "build"],
        env={**os.environ, "STUB_SLEEP": "30"}, stdout=subprocess.DEVNULL,
    )
    try:
        jobs.JOB_LOCK_PATH.write_text(json.dumps({"pid": orphan.pid, "jobId": "x" * 32}), encoding="utf-8")
        response = client.post("/api/jobs", json=JOB)
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert str(orphan.pid) in detail and "previous server run" in detail
        assert client.get("/api/areas").json()["count"] == 0  # nothing half-created
    finally:
        orphan.kill()
        orphan.wait()
    # the same lock with a dead PID is stale: removed and the job starts
    response = client.post("/api/jobs", json=JOB)
    assert response.status_code == 202
    wait_state(client, response.json()["id"])


def test_stale_or_foreign_lock_is_ignored(client):
    jobs = jobs_mod()
    # PID 1 is alive but is not the engine (PID reuse after a container restart)
    for content in (json.dumps({"pid": 1}), json.dumps({"pid": 999_999_999}), "garbage"):
        jobs.JOB_LOCK_PATH.write_text(content, encoding="utf-8")
        assert jobs.orphan_engine_pid() is None
        assert not jobs.JOB_LOCK_PATH.exists()


# ---- watchdog -------------------------------------------------------------------------------

def test_watchdog_terminates_silent_engine(client, monkeypatch):
    jobs = jobs_mod()
    monkeypatch.setattr(jobs, "IDLE_TIMEOUT_S", 1.0)
    monkeypatch.setattr(jobs, "TERM_GRACE_S", 2.0)
    monkeypatch.setenv("STUB_SLEEP", "30")
    started = client.post("/api/jobs", json=JOB).json()
    job = wait_state(client, started["id"], timeout=20)
    assert job["state"] == "error"
    assert "printed nothing" in job["error"] and "build" in job["error"]
    assert any("WATCHDOG" in line for line in job["log"])
    assert jobs._CURRENT_ID is None


# ---- providers -------------------------------------------------------------------------------

def test_probe_providers_handles_timeout_and_caches(client, monkeypatch):
    jobs = jobs_mod()
    calls = []

    def slow(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(cmd="python", timeout=60)

    monkeypatch.setattr(jobs.subprocess, "run", slow)
    result = jobs.probe_providers()
    assert result["fabdem"][0] is False and "timed out" in result["fabdem"][1]
    assert result["copernicus"][0] is True
    jobs.probe_providers()
    client.get("/api/fetch/providers")
    assert len(calls) == 1  # failure cached too: no re-spawn per request

    jobs._PROVIDER_CACHE = None

    def missing(*args, **kwargs):
        raise FileNotFoundError("no such interpreter")

    monkeypatch.setattr(jobs.subprocess, "run", missing)
    result = jobs.probe_providers()
    assert result["fabdem"][0] is False and "could not be started" in result["fabdem"][1]
