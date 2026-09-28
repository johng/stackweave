"""stackweave.optimize(*goals, max_fibers): one call, named trade-offs, that maps to
the numeric tuning knobs and the live spawn/scrub/cap settings.  Pins the
contract: valid goals, precedence (memory > throughput on the spawn path),
shell-env wins, and that the runtime still runs after a call.
"""
import os

import pytest

import stackweave
import stackweave_c
from stackweave._optimize import _GOAL_ENV, GOALS


_ENV_KNOBS = sorted({k for env in _GOAL_ENV.values() for k in env})


@pytest.fixture(autouse=True)
def _restore_settings(monkeypatch):
    # optimize() sets env knobs and flips process-wide live settings; start each
    # test from an unset env and put the defaults back afterwards so a later
    # test (and the conftest invariants) see an untouched runtime.
    for k in _ENV_KNOBS:
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    scrub = stackweave_c.get_stack_scrub()
    cap = stackweave_c.get_max_fibers()
    yield
    stackweave_c._fiber_set_speed(0)
    stackweave.set_grow_down(True)
    stackweave_c.set_stack_scrub(scrub)
    stackweave_c.set_max_fibers(cap)


def test_unknown_goal_raises():
    with pytest.raises(ValueError):
        stackweave.optimize("turbo")


def test_no_goals_applies_nothing():
    assert stackweave.optimize() == {}


def test_memory_keeps_grow_down():
    stackweave.set_grow_down(False)
    applied = stackweave.optimize("memory")
    assert applied == {"spawn": "grow_down"}
    assert stackweave.grow_down_enabled()


def test_throughput_bundle():
    applied = stackweave.optimize("throughput")
    assert applied["STACKWEAVE_BLOCKPOOL_WORKERS"] == "16"
    assert applied["spawn"] == "fast"


def test_latency_bundle():
    applied = stackweave.optimize("latency")
    assert applied == {"STACKWEAVE_SYSMON_MS": "25"}


def test_memory_wins_the_spawn_path_over_throughput():
    applied = stackweave.optimize("throughput", "memory")
    assert applied["STACKWEAVE_BLOCKPOOL_WORKERS"] == "16"     # from throughput
    assert applied["spawn"] == "grow_down"                     # memory wins


def test_secure_scrub_lands_when_composed():
    stackweave_c.set_stack_scrub(False)
    applied = stackweave.optimize("throughput", "secure")
    assert applied["stack_scrub"] is True
    assert stackweave_c.get_stack_scrub()


def test_max_fibers():
    applied = stackweave.optimize(max_fibers=12345)
    assert applied == {"max_fibers": 12345}
    assert stackweave_c.get_max_fibers() == 12345


def test_all_goal_values_are_well_formed():
    # every bundled value is a non-empty str (env vars), goals are the 4 trades.
    assert set(GOALS) == {"throughput", "latency", "memory", "secure"}
    for g, env in _GOAL_ENV.items():
        for k, v in env.items():
            assert k.startswith("STACKWEAVE_") and isinstance(v, str) and v


def test_shell_env_wins(monkeypatch):
    monkeypatch.setenv("STACKWEAVE_SYSMON_MS", "40")
    applied = stackweave.optimize("latency")        # wants 25
    assert os.environ["STACKWEAVE_SYSMON_MS"] == "40"   # explicit shell export wins
    assert applied["STACKWEAVE_SYSMON_MS"] == "40"


def test_runs_after_optimize():
    stackweave.optimize("memory")
    done = bytearray(200)

    def main():
        def w(i):
            done[i] = 1
        for i in range(200):
            stackweave.fiber(w, i)

    stackweave.run(4, main)
    assert sum(done) == 200
