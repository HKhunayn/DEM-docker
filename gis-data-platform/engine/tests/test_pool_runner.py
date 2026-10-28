"""pool_runner: per-tile error reporting, abort threshold, quarantine of a
tile that kills its worker, cgroup memory accounting."""

import pytest

import _dem_fixtures  # puts scripts/ on sys.path
import _pool_jobs as jobs
import pool_runner as pr

__all__ = ["_dem_fixtures"]  # imported for its sys.path side effect


def _run(job, tasks, workers=2):
    results, errors = [], []
    pr.run_pool(job, tasks, workers, jobs.init_noop, (), results.append, "t",
                on_error=lambda d, m: errors.append((d, m)))
    return results, errors


def test_per_tile_exception_reported_and_build_continues(capsys):
    tasks = [(1, x, 3) for x in range(10)] + [(2, 0, 0)]
    results, errors = _run(jobs.ok_or_raise, tasks)
    assert len(results) == 10
    assert len(errors) == 1
    desc, msg = errors[0]
    assert desc == "1/2/3" and "ValueError: synthetic failure" in msg
    assert "TILE FAILED 1/2/3" in capsys.readouterr().out


def test_abort_when_too_many_fail():
    tasks = [(3, x, 0) for x in range(40)]
    with pytest.raises(pr.PoolAborted, match="tiles failed"):
        _run(jobs.always_raise, tasks)


def test_tile_killing_worker_is_quarantined(capsys):
    tasks = [(4, x, 1) for x in range(12)]
    results, errors = _run(jobs.kill_on_5, tasks)
    out = capsys.readouterr().out
    assert sorted(r[2] for r in results) == [x for x in range(12) if x != 5]
    assert len(errors) == 1 and errors[0][0] == "4/5/1"
    assert "quarantined" in errors[0][1]
    assert "isolating" in out


def test_describe_task():
    assert pr.describe_task((13, 5142, 3520, "/out", None, False)) == "13/5142/3520"
    assert pr.describe_task("x") == "'x'"


def test_cgroup_used_counts_kernel(tmp_path):
    p = tmp_path / "memory.stat"
    p.write_text("anon 1000\nfile 99999\nkernel 300\nkernel_stack 10\nslab 50\n")
    assert pr.cgroup_used(str(p)) == 1300
    p.write_text("anon 1000\nfile 99999\nkernel_stack 10\nslab 50\npagetables 40\n")
    assert pr.cgroup_used(str(p)) == 1100
    assert pr.cgroup_used(str(tmp_path / "missing")) is None
