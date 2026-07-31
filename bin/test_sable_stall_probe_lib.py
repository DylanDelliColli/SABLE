"""Uniform pane-state interface tests for the stall-probe compatibility layer."""
from pathlib import Path
import inspect
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_pane_lib as pane  # noqa: E402
import sable_stall_probe_lib as stall  # noqa: E402
from test_sable_pane_lib import REAL_PANE_CAPTURES  # noqa: E402


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_deliberate_hold_classifies_real_provider_frames(provider):
    assert pane.deliberate_hold(REAL_PANE_CAPTURES[provider]["idle"], provider) is not None
    assert pane.deliberate_hold(REAL_PANE_CAPTURES[provider]["held"], provider) is None
    assert pane.deliberate_hold(REAL_PANE_CAPTURES[provider]["midturn"], provider) is None


def test_stall_probe_reexports_the_one_pane_state_implementation():
    assert stall.deliberate_hold is pane.deliberate_hold
    assert list(inspect.signature(stall.deliberate_hold).parameters) == [
        "capture",
        "provider",
    ]


def test_reexport_requires_the_real_provider_for_a_codex_capture():
    capture = REAL_PANE_CAPTURES["codex"]["idle"]

    assert stall.deliberate_hold(capture, "codex") is True
    assert stall.deliberate_hold(capture, "claude") is None
    with pytest.raises(TypeError):
        stall.deliberate_hold(capture)
