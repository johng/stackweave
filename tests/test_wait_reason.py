"""Wait-reason taxonomy: the deadlock/wedge dump subdivides the opaque
PARKED_SAFE "park" with the fiber's wait reason (future / waitgroup / lock /
...), set either explicitly via stackweave_c.set_wait_reason or by the high-level
sync primitives, so an operator can see WHY each fiber is blocked.

Driven through a subprocess because the dump is written straight to fd 2 by the
deadlock census; raise mode makes the run return promptly after the dump.
"""
import sys
import textwrap

import pytest

from adv_util import run_python


def _dump_for(body):
    code = textwrap.dedent("""
        import stackweave, stackweave_c
        from stackweave.sync import WaitGroup
        stackweave_c.set_deadlock_mode(2)   # raise: dump then return
        def main():
            {body}
        try:
            stackweave.run(2, main)
        except RuntimeError:
            pass
    """).format(body=body)
    p = run_python(code, timeout=40,
                   env={"STACKWEAVE_DEADLOCK_MS": "40", "PYTHONUNBUFFERED": "1",
                        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    return p.stdout + p.stderr


def test_explicit_set_wait_reason_shows_in_dump():
    out = _dump_for("stackweave_c.set_wait_reason(stackweave_c.WR_FUTURE); stackweave_c.park()")
    assert "park:future" in out, out


def test_waitgroup_primitive_tags_its_park():
    out = _dump_for("wg = WaitGroup(); wg.add(1); wg.wait()")
    assert "park:waitgroup" in out, out


def test_unset_reason_defaults_to_sync():
    out = _dump_for("stackweave_c.park()")
    assert "park:sync" in out, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
