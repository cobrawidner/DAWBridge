"""Confirm-before-push handoff, tested without a display or a DAW.

The dialog itself is Tk's; what's worth testing is the cross-thread
handshake around it, because the failure mode is a silent hang rather
than an error - the worker waits on a reply that never comes and every
button stays greyed out.
"""
import queue
import threading

from dawbridge.gui import DawBridgeGUI


class _FakeMaster:
    """Stands in for the Tk root: runs after() callbacks on a separate
    thread, the way the real main loop would.
    """

    def __init__(self):
        self.calls = []

    def after(self, _delay, fn):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        self.calls.append(t)


def _gui_stub(confirm_impl):
    gui = DawBridgeGUI.__new__(DawBridgeGUI)  # skip Tk widget construction
    gui.master = _FakeMaster()
    gui._confirm_push = confirm_impl
    gui._log = lambda msg: None
    return gui


def test_confirm_returns_the_users_answer():
    for answer in (True, False):
        gui = _gui_stub(lambda preview, daw, a=answer: a)
        assert gui._ask_confirm_on_main_thread(preview=None, daw="reaper") is answer


def test_confirm_failure_does_not_hang_and_defaults_to_no():
    # A dialog that raises must not leave the worker blocked forever.
    def boom(preview, daw):
        raise RuntimeError("Tk went away")

    gui = _gui_stub(boom)
    result = _with_timeout(lambda: gui._ask_confirm_on_main_thread(preview=None, daw="reaper"))
    assert result is False, "a failed confirmation must default to NOT writing to the DAW"


def _with_timeout(fn, seconds: float = 5.0):
    box: queue.Queue = queue.Queue(maxsize=1)
    t = threading.Thread(target=lambda: box.put(fn()), daemon=True)
    t.start()
    try:
        return box.get(timeout=seconds)
    except queue.Empty:  # pragma: no cover - only on regression
        raise AssertionError("confirmation handshake hung - the GUI would freeze here")
