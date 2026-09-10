"""CameraClient connection health against a scripted fake GUI server.

Run with:  python -m pytest tests/test_client_connection.py -v
"""

import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
)

from pirtcam.client import CameraClient  # noqa: E402


class FakeServer:
    """Accepts one connection; behavior chosen per test.

    mode:
      "echo"   - answer every command with a success dict
      "silent" - accept commands, never answer
      "close"  - close the connection as soon as a command arrives
    """

    def __init__(self, mode="echo"):
        self.mode = mode
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.conn = None
        self.commands = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            self.conn, _ = self.sock.accept()
        except OSError:
            return
        buf = b""
        while True:
            try:
                data = self.conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                cmd = json.loads(line)
                self.commands.append(cmd)
                if self.mode == "echo":
                    reply = {"status": "success", "echo": cmd.get("command")}
                    if cmd.get("command") == "GET_STATUS":
                        reply["data"] = {"camera_state": "READY"}
                    self.conn.sendall((json.dumps(reply) + "\n").encode())
                elif self.mode == "close":
                    self.conn.close()
                    return
                # "silent": do nothing

    def drop_client(self):
        if self.conn:
            self.conn.close()

    def close(self):
        self.drop_client()
        self.sock.close()


def test_get_status_roundtrip():
    srv = FakeServer("echo")
    c = CameraClient("127.0.0.1", srv.port)
    c.connect()
    try:
        assert c.is_connected()
        r = c.get_status()
        assert r["status"] == "success"
        assert r["data"]["camera_state"] == "READY"
    finally:
        c.disconnect()
        srv.close()
    assert not c.is_connected()


def test_connect_refused_raises_quickly():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nobody listening here now
    c = CameraClient("127.0.0.1", port, connect_timeout=2.0)
    t0 = time.time()
    with pytest.raises(OSError):
        c.connect()
    assert time.time() - t0 < 5
    assert not c.is_connected()
    with pytest.raises(ConnectionError):
        c.get_status()


def test_server_closing_mid_wait_fails_fast():
    srv = FakeServer("close")
    c = CameraClient("127.0.0.1", srv.port, response_timeout=30.0)
    c.connect()
    try:
        t0 = time.time()
        with pytest.raises(ConnectionError):
            c.send_command({"command": "GET_STATUS"})
        # must not wait out the 30 s response timeout
        assert time.time() - t0 < 5
        assert not c.is_connected()
        # subsequent commands fail immediately instead of hanging
        with pytest.raises(ConnectionError):
            c.get_status()
    finally:
        c.disconnect()
        srv.close()


def test_server_vanishing_between_commands_is_detected():
    srv = FakeServer("echo")
    c = CameraClient("127.0.0.1", srv.port)
    c.connect()
    try:
        assert c.get_status()["status"] == "success"
        srv.drop_client()
        t0 = time.time()
        while c.is_connected() and time.time() - t0 < 5:
            time.sleep(0.05)
        assert not c.is_connected()
        with pytest.raises(ConnectionError):
            c.get_status()
    finally:
        c.disconnect()
        srv.close()


def test_silent_server_times_out_with_error_dict():
    srv = FakeServer("silent")
    c = CameraClient("127.0.0.1", srv.port)
    c.connect()
    try:
        t0 = time.time()
        r = c.get_status(timeout=1.0)
        assert 0.9 < time.time() - t0 < 3
        assert r == {"status": "error", "message": "Response timeout"}
        # link itself is still up: a hung-but-connected GUI
        assert c.is_connected()
    finally:
        c.disconnect()
        srv.close()


def test_reconnect_replaces_old_session():
    srv = FakeServer("echo")
    c = CameraClient("127.0.0.1", srv.port)
    c.connect()
    first_thread = c.receive_thread
    srv.close()
    srv2 = FakeServer("echo")
    c.port = srv2.port
    c.connect()  # should tear down the first session cleanly
    try:
        assert c.receive_thread is not first_thread
        assert not first_thread.is_alive()
        assert c.get_status()["status"] == "success"
    finally:
        c.disconnect()
        srv2.close()
