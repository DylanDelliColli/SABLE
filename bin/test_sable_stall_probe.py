"""Unit tests for sable_stall_probe_lib.deliberate_hold (SABLE-cod50).

Guards the enumerate-vs-derive regression class directly: the original
deliberate_idle() matched a hardcoded list of hold PHRASES and false-STALLed
on optimus's real "standing down" hold because that exact wording wasn't in
the list. These fixtures build pane captures by SHAPE (settled composer with
turn content above it, vs. an unsent/unanswered wake, vs. a truncated turn)
and never assert on any particular wording, so a new phrasing can never
regress this the way it regressed the old marker list.
"""
import pytest

from sable_stall_probe_lib import deliberate_hold

_BORDER = "─" * 40
_CWD = "  ddc@host:~/wk-example"


def _settled_pane(content_lines):
    """A settled idle composer with `content_lines` rendered above it."""
    body = "\n".join(content_lines)
    return f"{body}\n{_BORDER}\n❯ \n{_BORDER}\n{_CWD}\n"


def _stuck_wake_pane(sender="lincoln", to="optimus", body="cap in force"):
    """A wake still sitting UNSENT in the composer (never submitted)."""
    return (
        f"● prior turn output\n{_BORDER}\n"
        f"❯ ⟦SABLE-MSG⟧ from={sender} to={to} :: {body}\n"
        f"{_BORDER}\n{_CWD}\n"
    )


def _truncated_pane():
    """Composer settled empty with NOTHING rendered above it."""
    return f"{_BORDER}\n❯ \n{_BORDER}\n{_CWD}\n"


def _busy_pane():
    return f"● doing work\n✻ Thinking… (12s · esc to interrupt)\n❯ \n"


def test_settled_hold_is_not_stall():
    """optimus's REAL hold text (SABLE-cod50 live false positive)."""
    pane = _settled_pane([
        "● Standing down — a wake will come from a landing, a message, or the",
        "  fresh Lincoln once it is booted. Recycle duty is complete and",
        "  verified... nothing is owed until morning.",
    ])
    assert deliberate_hold(pane) is True


def test_dropped_wake_is_stall():
    """Negative control: a delivered wake with no subsequent action."""
    assert deliberate_hold(_stuck_wake_pane()) is False


def test_truncated_turn_is_stall():
    """An empty, settled composer with no rendered turn content at all."""
    assert deliberate_hold(_truncated_pane()) is False


def test_session_limit_cut_is_stall():
    """A turn cut by the rate-limit banner still repaints a normal composer
    (SABLE-ita7) -- must not be misread as a completed, deliberate hold."""
    pane = _settled_pane(["You have hit your session limit - resets 2pm"])
    assert deliberate_hold(pane) is False


def test_busy_pane_is_not_assessed():
    assert deliberate_hold(_busy_pane()) is None


def test_no_composer_row_is_not_assessed():
    assert deliberate_hold("● some scrollback with no composer visible\n") is None


@pytest.mark.parametrize("phrase", [
    "Wrapping up for tonight — nothing left in my queue.",
    "Pausing here until the next signal arrives.",
    "Taking a break; ping me if something comes up.",
    "All caught up for now, going quiet.",
    "Calling it — I'll pick this back up when there's a reason to.",
])
def test_hold_phrasing_variants(phrase):
    """None of these phrasings appear in the OLD DELIBERATE_IDLE_MARKERS
    list this bead removed -- guards the enumeration regression class: any
    phrasing must classify as a hold, not just the ones on some list."""
    pane = _settled_pane([f"● {phrase}"])
    assert deliberate_hold(pane) is True
