#!/usr/bin/env python3
"""The suite's hermeticity contract (SABLE-4gxef).

This suite is routinely run from INSIDE a live SABLE pane, and much of it
resolves agent identity and fleet mode from the ambient environment. Before the
autouse fixture in conftest.py, that made results depend on who was running the
tests and what the fleet happened to be doing: measured 2026-07-29, the SAME
tree produced 59 failures inside the lincoln pane of an executing all-Codex
fleet and 0 failures with the ambient variables cleared.

That is the worst possible property for a commit gate — reddest exactly when
the fleet is busiest, so a real regression is indistinguishable from ambient
noise. Verifying that day's Codex fixes required a manual stash-and-compare
across 47 nodes purely to establish they had introduced nothing.

These tests pin the contract so the fixture cannot be quietly dropped or
narrowed. They are deliberately about the ENVIRONMENT a test starts in, not
about any one module's behaviour.
"""
import os

import pytest

import conftest


def test_ambient_fleet_identity_is_not_visible_to_tests():
    """No test may observe the identity of whatever agent launched pytest.
    A leaked CLAUDE_AGENT_NAME made hooks under test resolve the OPERATOR's
    identity (lincoln) instead of the one the test established."""
    for name in (
        "SABLE_AGENT_NAME",
        "CLAUDE_AGENT_NAME",
        "SABLE_AGENT_ROLE",
        "CLAUDE_AGENT_ROLE",
    ):
        assert os.environ.get(name) is None, (
            f"{name} leaked into the test environment — the suite is no longer "
            f"hermetic and will fail differently inside a live fleet"
        )


def test_ambient_tmux_client_is_not_visible_to_tests():
    """$TMUX makes tmux commands default to the OPERATOR's live session.
    Integration tests create their own sessions on explicit sockets; inheriting
    a client is how they reached into the real fleet."""
    assert os.environ.get("TMUX") is None


def test_mode_state_points_somewhere_that_does_not_exist():
    """Mode reads must report 'no execution session' rather than sampling the
    operator's live mode-state. Four tmux_integration cases asserted
    provider=claude and failed only because the real map said codex."""
    path = os.environ.get("SABLE_MODE_STATE")

    assert path, "SABLE_MODE_STATE must be set, not merely unset"
    assert not os.path.exists(path), (
        "SABLE_MODE_STATE must point at a path that does not exist, so mode "
        "reads fail closed to 'no session' instead of finding real state"
    )


def test_a_test_can_still_establish_its_own_identity(monkeypatch):
    """The fixture CLEARS rather than pins: a test that needs an identity sets
    its own and wins, because its setenv runs after the autouse fixture. Six
    modules already rely on this for SABLE_MODE_STATE."""
    monkeypatch.setenv("SABLE_AGENT_NAME", "chuck")

    assert os.environ["SABLE_AGENT_NAME"] == "chuck"


def test_the_cleared_set_is_declared_not_incidental():
    """The variable list is a stated contract. If a new ambient fleet variable
    is introduced, it belongs here — this assertion exists so that addition is
    a deliberate edit rather than a silent omission."""
    assert set(conftest._AMBIENT_FLEET_ENV) == {
        "SABLE_AGENT_NAME",
        "CLAUDE_AGENT_NAME",
        "SABLE_AGENT_ROLE",
        "CLAUDE_AGENT_ROLE",
        "SABLE_PROVIDER",
        "SABLE_BEAD",
        "SABLE_WORKER_PANE",
        "TMUX",
    }


@pytest.mark.parametrize("name", ["SABLE_AGENT_NAME", "TMUX"])
def test_clearing_survives_into_subprocess_environment(name, tmp_path):
    """NEGATIVE CONTROL that the clearing is real rather than cosmetic: a
    subprocess inheriting os.environ must not see the variable either. Hooks
    under test are invoked as subprocesses, so this is the property that
    actually mattered."""
    import subprocess

    result = subprocess.run(
        ["bash", "-c", f'printf "%s" "${{{name}-__ABSENT__}}"'],
        capture_output=True,
        text=True,
    )

    assert result.stdout == "__ABSENT__"
