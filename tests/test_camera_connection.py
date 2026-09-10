"""Exercise SciCamGUI's disconnected-camera handling against a fake serial link.

gui.py can't be imported off-instrument (BitFlow DLLs, Windows-only os call),
so the instrument-only modules are stubbed and the real SciCamGUI methods are
bound to a mock object, the same approach as test_timing_logic.py. The module's
``time`` and ``QTimer`` are replaced with fakes so serial timeouts and deferred
callbacks run instantly and deterministically.

Run with:  python -m pytest tests/test_camera_connection.py -v
"""

import json
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

# --- stub the Windows/instrument-only bits so gui.py imports ---------------
os.add_dll_directory = lambda path: None


def stub_module(name):
    mod = types.ModuleType(name)
    mod.__file__ = f"<stub {name}>"

    def _getattr(attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return types.SimpleNamespace()

    mod.__getattr__ = _getattr
    sys.modules[name] = mod
    return mod


bf = stub_module("BFModule")
bf.BufferAcquisition = stub_module("BFModule.BufferAcquisition")
bf.CLComm = stub_module("BFModule.CLComm")
stub_module("pyqtgraph")

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
)

import pirtcam.gui as gui  # noqa: E402
from pirtcam.gui import CameraState, SciCamGUI  # noqa: E402


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeTime:
    """Deterministic clock: sleep() advances time() instantly."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


class FakeQTimer:
    """Records singleShot callbacks instead of scheduling them."""

    pending = []

    @classmethod
    def singleShot(cls, ms, callback):
        cls.pending.append(callback)

    @classmethod
    def run_pending(cls):
        cbs, cls.pending = cls.pending, []
        for cb in cbs:
            cb()


class FakeCL:
    """Camera Link serial stand-in. ``replies`` maps command -> reply text;
    an absent command (or ``silent``) yields no bytes, like a powered-off
    camera."""

    def __init__(self, replies=None, silent=False):
        self.replies = replies or {}
        self.silent = silent
        self.written = []
        self._pending = b""

    def SerialWrite(self, text, timeout):
        cmd = text.strip()
        self.written.append(cmd)
        if not self.silent and cmd in self.replies:
            self._pending = (self.replies[cmd] + "\r\nOK\r\n>").encode()

    def SerialRead(self, n, size):
        out, self._pending = self._pending, b""
        return out

    def SerialClose(self):
        pass


CAMERA_REPLIES = {
    "SYS:MODEL?": "PIRT1280",
    "SENS:EXPPER?": "1000000",
    "SENS:FRAMEPER?": "1100000",
    "TEMP:SENS:SET?": "-60.0",
    "TEMP:SENS?": "-60.01",
}


class MockGUI:
    """Minimal SciCamGUI stand-in wired to a FakeCL."""

    def __init__(self, cl):
        self.CL = cl
        self.terminal = []
        self.serial_init_count = 0

        # attributes the bound methods touch
        self.serial_error_count = 0
        self.max_serial_errors = 3
        self.state = {}
        self.camera_connected = False
        self._disconnect_declared = False
        self._needs_timing_resync = True
        self._reconnecting = False
        self._last_probe_time = 0.0
        self.is_capturing = False
        self.waiting_on_exposure_update = False
        self.current_frame = 0
        self.total_frames = 0
        self.frame_start_time = None
        self.last_exposure_duration = None
        self.current_exposure_time = 1.0
        self.current_frame_time = 1.1
        self.fixed_frame_time = 1.1
        self.frame_time_overhead = 0.1
        self.frame_time_mode = gui.FrameTimeMode.AUTO
        self.trigmode = gui.TrigMode.SINGLE
        self.camera_state = CameraState.READY
        self.command_server = MagicMock()

        # widgets
        for name in [
            "exp_input", "nframes_input", "object_input", "observer_input",
            "save_path_input", "capture_button", "tec_lock_light",
            "camera_conn_label", "camera_state_label", "ready_light",
            "timeout_label", "last_exposure_label", "capture_status_label",
            "capture_progress_label", "capture_progress",
        ]:
            setattr(self, name, MagicMock())
        self.exp_input.value.return_value = 1.0
        self.nframes_input.text.return_value = "1"
        for name in ("object_input", "observer_input", "save_path_input"):
            getattr(self, name).text.return_value = ""

    # stubs for things we don't exercise
    def print_terminal(self, msg):
        self.terminal.append(msg)

    def setup_serial(self):
        self.serial_init_count += 1

    def _sync_timing_display(self):
        pass

    # real implementations under test
    CAMERA_PROBE_INTERVAL_S = SciCamGUI.CAMERA_PROBE_INTERVAL_S
    CAMERA_COMMANDS = SciCamGUI.CAMERA_COMMANDS
    query_scalar = SciCamGUI.query_scalar
    _query_cycles = SciCamGUI._query_cycles
    _note_serial_failure = SciCamGUI._note_serial_failure
    _set_camera_connected = SciCamGUI._set_camera_connected
    _set_camera_connection_label = SciCamGUI._set_camera_connection_label
    attempt_serial_reconnect = SciCamGUI.attempt_serial_reconnect
    _probe_camera = SciCamGUI._probe_camera
    _load_timing_from_camera = SciCamGUI._load_timing_from_camera
    _on_camera_reconnected = SciCamGUI._on_camera_reconnected
    _initial_camera_contact = SciCamGUI._initial_camera_contact
    update_status_indicators = SciCamGUI.update_status_indicators
    update_time_fields = SciCamGUI.update_time_fields
    process_remote_command = SciCamGUI.process_remote_command


@pytest.fixture
def clock(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(gui, "time", fake)
    monkeypatch.setattr(gui, "QTimer", FakeQTimer)
    FakeQTimer.pending = []
    return fake


def last_reply(g):
    """Parse the last JSON reply sent through the command server."""
    args, _ = g.command_server.send_response.call_args
    return json.loads(args[0].strip())


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test_startup_with_camera_off_reports_disconnected_quickly(clock):
    g = MockGUI(FakeCL(silent=True))
    t0 = clock.time()
    g._initial_camera_contact()

    assert g.camera_connected is False
    assert g.state["camera_connected"] is False
    # only the two timing reads + one reconnect test query, not a full refresh
    assert g.CL.written.count("SYS:MODEL?") == 1
    assert clock.time() - t0 < 10.0  # a handful of 2 s timeouts, not ~30 s
    assert g.serial_init_count == 1  # one reconnect attempt, no recursion


def test_disconnect_after_threshold_without_recursion(clock):
    g = MockGUI(FakeCL(silent=True))
    g.camera_connected = True
    g._needs_timing_resync = False

    g.query_scalar("TEMP:SENS?")
    g.query_scalar("TEMP:SENS?")
    assert g.camera_connected is True  # below threshold
    g.query_scalar("TEMP:SENS?")  # third failure
    assert g.camera_connected is False
    assert g.serial_init_count == 1
    assert g.state["tec_lock"] == 0
    g.capture_button.setEnabled.assert_called_with(False)

    # further failures while disconnected do not trigger more reconnects
    for _ in range(5):
        g.query_scalar("TEMP:SENS?")
    assert g.serial_init_count == 1


def test_status_refresh_probes_on_slow_cadence_while_disconnected(clock):
    g = MockGUI(FakeCL(silent=True))
    g._set_camera_connected(False, "test")
    probe_t0 = g._last_probe_time

    # within the probe interval: no serial traffic at all
    g.update_status_indicators()
    g.update_status_indicators()
    assert g.serial_init_count == 0
    assert g.CL.written == []

    # after the interval: exactly one probe (reconnect + SYS:MODEL?)
    clock.now = probe_t0 + g.CAMERA_PROBE_INTERVAL_S + 0.1
    g.update_status_indicators()
    assert g.serial_init_count == 1
    assert g.CL.written == ["SYS:MODEL?"]
    assert g.camera_connected is False


def test_reconnect_restores_state_and_resyncs_timing(clock):
    g = MockGUI(FakeCL(silent=True))
    g._set_camera_connected(False, "test")
    clock.now += g.CAMERA_PROBE_INTERVAL_S + 1

    # camera comes back
    g.CL.silent = False
    g.CL.replies = dict(CAMERA_REPLIES)
    g.update_status_indicators()  # probe succeeds
    assert g.camera_connected is True
    assert g.state["camera_connected"] is True
    assert len(FakeQTimer.pending) == 1  # resync scheduled, not run inline

    g.update_status_indicators = lambda: g.terminal.append("full refresh")
    FakeQTimer.run_pending()
    assert "full refresh" in g.terminal
    assert g._needs_timing_resync is False
    g.exp_input.setValue.assert_called_with(gui.cycles_to_sec(1000000))
    assert g.current_frame_time == gui.cycles_to_sec(1100000)


def test_remote_camera_commands_rejected_while_disconnected(clock):
    g = MockGUI(FakeCL(silent=True))
    g._set_camera_connected(False, "test")
    t0 = clock.time()

    for cmd in ("CAPTURE", "SET_EXPOSURE", "SET_TEC_TEMP", "TEC_EN", "SET_CORRECTION"):
        g.process_remote_command(json.dumps({"command": cmd, "exposure": 2.0}))
        reply = last_reply(g)
        assert reply["status"] == "error"
        assert "camera not connected" in reply["message"]
    assert clock.time() == t0  # answered immediately, no serial timeouts
    assert g.CL.written == []

    # GET_STATUS still works and tells the truth
    g.update_time_fields()
    g.process_remote_command(json.dumps({"command": "GET_STATUS"}))
    reply = last_reply(g)
    assert reply["status"] == "success"
    assert reply["data"]["camera_connected"] is False
    assert reply["data"]["camera_state"] == "ERROR"
    assert reply["data"]["ready"] is False


def test_time_fields_follow_connection_state(clock):
    g = MockGUI(FakeCL())
    g.state["tec_lock"] = 1

    g.camera_connected = False
    g.update_time_fields()
    assert g.camera_state == CameraState.ERROR
    assert g.state["ready"] is False

    g.camera_connected = True
    g.update_time_fields()
    assert g.camera_state == CameraState.READY
    assert g.state["ready"] is True
