"""Real-tmux byte-exact coverage for ``deliver_text``'s buffer transport."""
import importlib.util
import shlex
import shutil
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_pane_lib as lib  # noqa: E402


HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")

_MSG_LOADER = SourceFileLoader(
    "sable_msg_for_pane_integration",
    str(Path(__file__).resolve().parent / "sable-msg"),
)
_MSG_SPEC = importlib.util.spec_from_loader(_MSG_LOADER.name, _MSG_LOADER)
sable_msg = importlib.util.module_from_spec(_MSG_SPEC)
_MSG_LOADER.exec_module(sable_msg)


def _tmux(socket, *args, check=True):
    return subprocess.run(
        ["tmux", "-L", socket, *args], capture_output=True, text=True,
        check=check,
    )


@pytest.fixture
def scratch_tmux(tmp_path):
    socket = f"sable-buffer-{time.monotonic_ns()}"
    yield socket, tmp_path
    _tmux(socket, "kill-server", check=False)


def _start_bracketed_paste_recorder(socket, output_path):
    program = (
        "import os,sys,time,tty; tty.setraw(0); "
        "os.write(1,b'\\x1b[?2004hREADY'); data=b''; "
        "end=b'\\x1b[201~'; "
        "\nwhile end not in data: data += os.read(0,65536)"
        "\npayload=data.split(b'\\x1b[200~',1)[1].split(end,1)[0]"
        "\nopen(sys.argv[1],'wb').write(payload)"
        "\ntime.sleep(5)"
    )
    command = shlex.join([sys.executable, "-c", program, str(output_path)])
    _tmux(socket, "new-session", "-d", "-s", "target", command)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        pane = _tmux(socket, "capture-pane", "-p", "-t", "target").stdout
        if "READY" in pane:
            return
        time.sleep(0.01)
    raise AssertionError("bracketed-paste recorder did not become ready")


@pytest.mark.parametrize(
    "body", ["x" * 20_000, "final-byte;"],
    ids=["above-send-keys-ceiling", "trailing-semicolon"],
)
def test_buffer_transport_delivers_payload_byte_exact(scratch_tmux, body):
    socket, tmp_path = scratch_tmux
    output = tmp_path / "received.bin"
    _start_bracketed_paste_recorder(socket, output)
    base = ["tmux", "-L", socket]
    pasted = False

    def run(cmd):
        nonlocal pasted
        result = subprocess.run(cmd, capture_output=True, text=True)
        if "paste-buffer" in cmd and result.returncode == 0:
            pasted = True
        return result.returncode == 0

    def capture():
        if not pasted:
            return "❯ "
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not output.exists():
            time.sleep(0.01)
        return f"❯ {body}\n● accepted\n❯ "

    assert lib.deliver_text(
        base, "target", body, body, tries=1, interval=0,
        run=run, capture=capture, sleep=time.sleep,
    )
    received = output.read_bytes()
    assert len(received) == len(body.encode())
    assert received == body.encode()


def test_multiline_refusal_prevents_raw_paste_into_unprotected_pane(
        scratch_tmux):
    socket, tmp_path = scratch_tmux
    output = tmp_path / "raw-input.bin"
    command = f"cat > {shlex.quote(str(output))}"
    _tmux(socket, "new-session", "-d", "-s", "unprotected", command)

    with pytest.raises(sable_msg.UnsafeMultilineMessage, match="multi-line body"):
        sable_msg.format_message("optimus", "chuck", "first\nsecond", 0.0)

    time.sleep(0.05)
    assert not output.exists() or output.read_bytes() == b""
