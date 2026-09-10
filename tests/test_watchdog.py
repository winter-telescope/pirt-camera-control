"""GUI watchdog: probe behavior, restart policy, config handling.

Run with:  python -m pytest tests/test_watchdog.py -v
"""

import json
import os
import socket
import sys
import threading

import pytest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
)

from pirtcam.watchdog import (  # noqa: E402
    DEFAULT_GUI_PATTERNS,
    GuiSupervisor,
    ProbeResult,
    SupervisorConfig,
    build_parser,
    config_from_args,
    probe_gui,
)


# ---------------------------------------------------------------------------
# probe_gui against tiny fake servers
# ---------------------------------------------------------------------------
def _serve_once(handler):
    """Start a one-shot TCP server; handler(conn) runs in a thread."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        conn, _ = srv.accept()
        try:
            handler(conn)
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def test_probe_ok_on_status_reply():
    def handler(conn):
        conn.recv(4096)
        conn.sendall(
            b'{"status": "success", "data": {"camera_state": "ERROR", "camera_connected": false}}\n'
        )

    port = _serve_once(handler)
    r = probe_gui("127.0.0.1", port, timeout=3)
    assert r.ok  # camera off is the GUI's problem, not a reason to restart it
    assert r.data["camera_connected"] is False


def test_probe_skips_event_notifications_and_accepts_error_replies():
    def handler(conn):
        conn.recv(4096)
        conn.sendall(b'{"event": "frame_saved"}\n{"status": "error", "message": "TEC not locked"}\n')

    port = _serve_once(handler)
    r = probe_gui("127.0.0.1", port, timeout=3)
    assert r.ok  # any real reply proves the GUI thread is alive


def test_probe_fails_when_nobody_listens():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = probe_gui("127.0.0.1", port, timeout=2)
    assert not r.ok
    assert "connect" in r.detail


def test_probe_fails_on_silence():
    def handler(conn):
        conn.recv(4096)
        threading.Event().wait(2.5)  # never answer

    port = _serve_once(handler)
    r = probe_gui("127.0.0.1", port, timeout=1)
    assert not r.ok
    assert "no reply" in r.detail


# ---------------------------------------------------------------------------
# GuiSupervisor policy with fakes
# ---------------------------------------------------------------------------
class FakeProc:
    def __init__(self, pid):
        self.pid = pid


class Harness:
    def __init__(self, **cfg_overrides):
        cfg = SupervisorConfig(
            interval_s=10,
            hang_timeout_s=180,
            startup_grace_s=120,
            restart_cooldown_s=60,
            launch_cmd=["fake-gui"],
        )
        for k, v in cfg_overrides.items():
            setattr(cfg, k, v)
        self.now = 1000.0
        self.probe_ok = True
        self.procs = [FakeProc(4242)]
        self.killed = []
        self.launched = 0
        self.sup = GuiSupervisor(
            cfg,
            probe_fn=lambda: ProbeResult(self.probe_ok, "fake"),
            find_procs_fn=lambda: list(self.procs),
            kill_fn=self._kill,
            launch_fn=self._launch,
            clock=lambda: self.now,
            sleep=lambda s: None,
        )

    def _kill(self, procs):
        self.killed.append([p.pid for p in procs])
        self.procs = []

    def _launch(self):
        self.launched += 1
        self.procs = [FakeProc(5000 + self.launched)]
        return None

    def tick(self, dt=10):
        self.now += dt
        return self.sup.run_once()


def test_healthy_gui_is_left_alone():
    h = Harness()
    for _ in range(20):
        h.tick()
    assert h.launched == 0 and h.killed == []
    assert h.sup.state.last_probe_ok is True


def test_dead_process_restarts_immediately():
    h = Harness()
    h.tick()
    h.procs = []
    h.probe_ok = False
    h.tick()
    assert h.launched == 1
    assert h.killed == []  # nothing to kill
    assert "no GUI process" in h.sup.state.last_restart_reason


def test_hung_process_restarts_only_after_hang_timeout():
    h = Harness()
    h.tick()
    h.probe_ok = False  # process alive, not answering
    # first failing tick starts the clock; k failing ticks => (k-1)*10 s down
    for _ in range(18):  # 170 s of silence: below 180 s threshold
        h.tick()
    assert h.launched == 0
    h.tick()  # 180 s
    assert h.launched == 1
    assert h.killed == [[4242]]
    assert "unresponsive" in h.sup.state.last_restart_reason


def test_recovery_resets_hang_clock():
    h = Harness()
    h.tick()
    h.probe_ok = False
    for _ in range(10):
        h.tick()
    h.probe_ok = True
    h.tick()
    assert h.sup.state.unhealthy_since is None
    h.probe_ok = False
    for _ in range(17):
        h.tick()
    assert h.launched == 0  # clock restarted from the recovery


def test_startup_grace_and_cooldown_prevent_restart_loops():
    h = Harness()
    h.procs = []
    h.probe_ok = False
    h.tick()
    assert h.launched == 1
    # the new GUI takes a while to bind its port: still failing probes
    for _ in range(11):  # 110 s, inside 120 s grace
        h.tick()
    assert h.launched == 1
    # past grace the hang clock (started at the first failure after launch)
    # has to reach hang_timeout before the next restart
    for _ in range(18):
        h.tick()
    assert h.launched == 2


def test_dry_run_never_kills_or_launches():
    h = Harness(dry_run=True)
    h.procs = []
    h.probe_ok = False
    h.tick()
    assert h.launched == 0 and h.killed == []
    assert h.sup.state.restarts_total == 1  # decision is still recorded


def test_pause_file_stands_down(tmp_path):
    pause = tmp_path / "watchdog.pause"
    h = Harness(pause_file=str(pause))
    h.procs = []
    h.probe_ok = False
    pause.write_text("")
    assert h.tick() is None
    assert h.launched == 0
    pause.unlink()
    h.tick()
    assert h.launched == 1


def test_status_file_written(tmp_path):
    status = tmp_path / "status.json"
    h = Harness(status_file=str(status))
    h.tick()
    payload = json.loads(status.read_text())
    assert payload["last_probe_ok"] is True
    assert payload["gui_pids"] == [4242]


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def test_config_file_and_cli_override(tmp_path):
    launcher = tmp_path / "start gui.bat"  # path with a space, given as a string
    launcher.write_text("@echo off\n")
    cfg_path = tmp_path / "watchdog.json"
    cfg_path.write_text(json.dumps({
        "launch": str(launcher),
        "port": 5556,
        "hang_timeout": 300,
        "log_dir": str(tmp_path / "logs"),
    }))
    args = build_parser().parse_args(["--config", str(cfg_path), "--port", "5557"])
    cfg = config_from_args(args)
    assert cfg.launch_cmd == [str(launcher)]  # existing file: used verbatim
    assert cfg.port == 5557  # CLI wins over file
    assert cfg.hang_timeout_s == 300
    assert cfg.interval_s == 10.0  # default
    assert cfg.log_dir == str(tmp_path / "logs") and os.path.isdir(cfg.log_dir)
    assert cfg.pause_file == os.path.join(cfg.log_dir, "watchdog.pause")
    assert cfg.status_file == os.path.join(cfg.log_dir, "watchdog_status.json")
    assert cfg.gui_patterns == DEFAULT_GUI_PATTERNS


def test_config_launch_as_list_and_unknown_key(tmp_path):
    cfg_path = tmp_path / "watchdog.json"
    cfg_path.write_text(json.dumps({"launch": ["python", "-m", "pirtcam.gui", "-t", "SINGLE"]}))
    cfg = config_from_args(build_parser().parse_args(["--config", str(cfg_path)]))
    assert cfg.launch_cmd == ["python", "-m", "pirtcam.gui", "-t", "SINGLE"]

    cfg_path.write_text(json.dumps({"lauch": "typo"}))
    with pytest.raises(ValueError, match="unknown keys"):
        config_from_args(build_parser().parse_args(["--config", str(cfg_path)]))


def test_no_config_defaults():
    cfg = config_from_args(build_parser().parse_args([]))
    assert cfg.launch_cmd[1:] == ["-m", "pirtcam.gui"]
    assert cfg.port == 5555 and cfg.pause_file is None
