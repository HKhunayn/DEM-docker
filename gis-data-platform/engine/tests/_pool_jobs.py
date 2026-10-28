"""Picklable jobs for the pool_runner tests (spawned workers import this)."""

import os


def init_noop(*_args):
    pass


def ok_or_raise(task):
    z, x, y = task[:3]
    if (z, x, y) == (1, 2, 3):
        raise ValueError("synthetic failure")
    return ("ok", z, x, y)


def always_raise(task):
    raise RuntimeError(f"boom {task[:3]}")


def kill_on_5(task):
    if task[1] == 5:
        os._exit(13)  # simulates the OOM killer taking the worker down
    return ("ok",) + tuple(task[:3])
