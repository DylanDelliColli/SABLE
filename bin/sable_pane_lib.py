"""sable_pane_lib — shared tmux-pane readiness + dispatch helpers (SABLE-bldh.14).

Used by both `sable-spawn-worker` (deliver a worker's dispatch prompt) and
`sable-tmux --autostart` (kick a role pane's operating loop). The hard parts —
beating the boot/dropped-Enter race and accepting blocking startup gates (the
bypass-permissions warning, the trust dialog) — live here once.

Both importers are symlinks on PATH, so they resolve this module via
`os.path.realpath(__file__)`'s directory; no separate install step is needed.
"""
from __future__ import annotations

import subprocess
import time


def pane_ready(capture: str) -> bool:
    """The TUI is ready to accept input once its input box shows an EMPTY prompt
    line (just the prompt glyph). While booting (splash) or on a blocking gate
    screen there is no such empty line."""
    for line in reversed(capture.splitlines()):
        s = line.strip()
        if s in ("❯", ">"):
            return True
    return False


def accept_startup_gate(capture: str) -> str | None:
    """The menu key that ACCEPTS a known blocking startup gate, or None. The
    bypass-permissions warning defaults to '1. No, exit' (so a blind Enter kills
    the session), and a fresh worktree may show a trust dialog. Accept bypass with
    '2' (Yes, I accept) and trust with '1' (Yes, I trust this folder)."""
    low = capture.lower()
    if "bypass permissions mode" in low and "accept" in low:
        return "2"
    if "trust this folder" in low or ("do you trust" in low and "yes" in low):
        return "1"
    return None


def dispatch_landed(capture: str, snippet: str) -> bool:
    """True once the instruction has been SUBMITTED: the snippet appears in the
    pane but no longer sits in the (last) input-box prompt line. If it is still in
    the input box, the Enter was dropped and must be resent."""
    if snippet not in " ".join(capture.split()):
        return False
    box = None
    for line in capture.splitlines():
        s = line.strip()
        if s.startswith("❯") or s.startswith(">"):
            box = s
    if box is None:
        return True
    return snippet not in box


def capture_pane(base: list[str], pane: str) -> str:
    return subprocess.run(base + ["capture-pane", "-p", "-t", pane],
                          capture_output=True, text=True).stdout


def wait_for_ready(base, pane, timeout, interval=0.5, capture=None, sleep=None,
                   run=None) -> bool:
    """Poll until the pane shows its empty prompt, accepting any blocking startup
    gate (bypass warning / trust dialog) on the way so the session doesn't die on
    the gate's default 'No, exit'."""
    capture = capture or (lambda: capture_pane(base, pane))
    sleep = sleep or time.sleep
    # tolerant runner: a dead pane must not crash the caller
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True).returncode == 0)
    waited = 0.0
    while waited < timeout:
        cap = capture()
        if pane_ready(cap):
            return True
        key = accept_startup_gate(cap)
        if key is not None:
            run(base + ["send-keys", "-t", pane, key])
            run(base + ["send-keys", "-t", pane, "Enter"])
        sleep(interval)
        waited += interval
    return False


def deliver_text(base, pane, text, snippet, tries=8, interval=1.0,
                 run=None, capture=None, sleep=None) -> bool:
    """Type `text` into the pane, then submit — resending Enter until the text
    leaves the input box (the dropped-Enter race). A resent Enter on an already-
    empty box is a harmless no-op. Returns False (clean) if the pane vanishes."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True).returncode == 0)
    capture = capture or (lambda: capture_pane(base, pane))
    sleep = sleep or time.sleep
    if run(base + ["send-keys", "-t", pane, "-l", text]) is False:
        return False
    for _ in range(max(1, tries)):
        sleep(interval)
        if dispatch_landed(capture(), snippet):
            return True
        if run(base + ["send-keys", "-t", pane, "Enter"]) is False:
            return False
    return dispatch_landed(capture(), snippet)
