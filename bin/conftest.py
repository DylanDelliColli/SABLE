"""conftest for bin/ — the PYTHON HALF of the suite-run interlock (SABLE-pk15w).

This file is the whole reason the python half needs no runner enumeration.
pytest loads the conftest for any collection under bin/ REGARDLESS of who
invoked it — ci-verify, .github/ci/diff-cover-gate.sh, the impact tier, a
future cadence-snapshot runner, or an operator typing `pytest bin/` by hand —
so every one of them registers by construction. Adding a new pytest-based
runner therefore cannot open the hole SABLE-pk15w describes: there is nothing
to remember to add anywhere.

It also closes the harder of the two ps failure modes for free. `pytest bin/`
LOADS bins like sable-spawn-worker in-process; such a run is INVISIBLE to any
process-table probe for its entire duration, which is how three independently
written probes returned zero and all three were meaningless. A registration
held for the session is visible whether or not anything ever forks.

FAILURE POLICY. If the registry cannot be resolved or written we warn LOUDLY
and let the session run. That is not a silent fallback: clearance fails closed
on the SAME condition (an unresolvable or unwritable registry is
could-not-assess, never clear), so there is no configuration in which this
session goes unrecorded while the seat reads CLEAR — see the module docstring
of bin/sable_runlock_lib.py.
"""
from __future__ import annotations

import importlib.util
import signal
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "sable_runlock_lib.py"
_spec = importlib.util.spec_from_file_location("sable_runlock_lib", _LIB)
_runlock = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("sable_runlock_lib", _runlock)
_spec.loader.exec_module(_runlock)

_TOKEN: str | None = None


def _release_on_sigterm(signum, frame):
    """Turn a SIGTERM into an orderly exit so pytest still runs
    pytest_sessionfinish and the registration is released.

    Measured, not assumed: a `timeout 900 pytest bin/` that expires SIGTERMs the
    session, sessionfinish never runs, and the entry reads STALE afterwards —
    correct fail-closed behaviour, but it makes an ordinary CI timeout look
    identical to a crashed runner and trains people to reap reflexively. This is
    the exact counterpart of the INT/TERM/HUP traps in shell-run-set.sh's
    runlock_hold, so both halves of the interlock behave the same way. A SIGKILL
    still leaves the entry stale — that half stays fail-closed on purpose."""
    raise SystemExit(128 + signum)


def pytest_sessionstart(session):
    global _TOKEN
    try:
        signal.signal(signal.SIGTERM, _release_on_sigterm)
    except (ValueError, OSError):
        pass        # not the main thread, or a platform without SIGTERM
    try:
        _TOKEN = _runlock.register(
            f"pytest ({' '.join(sys.argv[1:]) or 'bin/'})",
            base=str(Path(__file__).resolve().parent),
        )
    except Exception as exc:  # noqa: BLE001 — never break a test session over this
        _TOKEN = None
        print(f"::warning::sable run registry: this pytest session is NOT "
              f"registered ({exc}). Hot-swap clearance will report "
              f"could-not-assess rather than clear (SABLE-pk15w).", file=sys.stderr)


def pytest_sessionfinish(session, exitstatus):
    global _TOKEN
    if _TOKEN is None:
        return
    try:
        _runlock.release(_TOKEN, base=str(Path(__file__).resolve().parent))
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::sable run registry: could not release {_TOKEN} ({exc}); "
              f"it will read STALE until reaped.", file=sys.stderr)
    finally:
        _TOKEN = None
