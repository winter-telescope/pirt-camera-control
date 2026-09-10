"""CommandServer: replies go to the requesting client, notifications to all.

Run with:  python -m pytest tests/test_command_server.py -v
"""

import json
import os
import socket
import sys
import threading
import time

import pytest
from PyQt5.QtCore import QCoreApplication

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
)

from pirtcam.command_server import CommandServer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture
def server(qapp):
    srv = CommandServer(port=0, host="127.0.0.1")
    received = []
    lock = threading.Lock()

    def on_command(command, client_id):
        with lock:
            received.append((json.loads(command), client_id))
        # echo a reply that names the requester so the test can tell replies apart
        srv.send_response(json.dumps({"status": "success", "for": client_id}), client_id)

    srv.command_received.connect(on_command)
    srv.start()
    assert srv.ready.wait(5), "server did not start"
    srv.received = received
    yield srv
    srv.stop()
    srv.wait(2000)


_buffers = {}  # socket -> unread bytes


def _client(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.settimeout(0.05)
    _buffers[s] = b""
    return s


def _read_line(sock, timeout=5):
    """Read one JSON line while pumping the Qt event loop.

    command_received is delivered to the (main-thread) slot through Qt's
    queued connection, exactly as in the real GUI, so the test has to run
    the event loop for the reply to be produced.
    """
    buf = _buffers.get(sock, b"")
    deadline = time.time() + timeout
    while b"\n" not in buf:
        QCoreApplication.processEvents()
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            if time.time() > deadline:
                raise TimeoutError("no line received")
            continue
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    line, rest = buf.split(b"\n", 1)
    _buffers[sock] = rest  # keep anything that arrived in the same packet
    return json.loads(line.decode())


def _expect_silence(sock, duration=0.5):
    deadline = time.time() + duration
    while time.time() < deadline:
        QCoreApplication.processEvents()
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        raise AssertionError(f"unexpected data: {chunk!r}")


def _wait_clients(srv, n, timeout=5):
    t0 = time.time()
    while srv.client_count < n and time.time() - t0 < timeout:
        time.sleep(0.02)
    assert srv.client_count == n


def test_reply_only_reaches_requester(server):
    a = _client(server.bound_port)
    b = _client(server.bound_port)
    _wait_clients(server, 2)

    a.sendall(b'{"command": "GET_STATUS"}\n')
    reply = _read_line(a)
    assert reply["status"] == "success"

    # b must NOT have received a's reply
    _expect_silence(b)

    # and b's own command gets b's own reply
    b.sendall(b'{"command": "GET_STATUS"}\n')
    reply_b = _read_line(b)
    assert reply_b["for"] != reply["for"]
    a.close()
    b.close()


def test_broadcast_reaches_everyone(server):
    a = _client(server.bound_port)
    b = _client(server.bound_port)
    _wait_clients(server, 2)

    server.broadcast({"event": "frame_saved", "filename": "x.fits"})
    assert _read_line(a)["event"] == "frame_saved"
    assert _read_line(b)["event"] == "frame_saved"

    # legacy call shape (no client_id) still broadcasts
    server.send_response(json.dumps({"event": "capture_complete"}) + "\n")
    assert _read_line(a)["event"] == "capture_complete"
    assert _read_line(b)["event"] == "capture_complete"
    a.close()
    b.close()


def test_partial_and_multiple_commands_per_packet(server):
    a = _client(server.bound_port)
    _wait_clients(server, 1)
    a.sendall(b'{"command": "GET_ST')
    time.sleep(0.1)
    a.sendall(b'ATUS"}\n{"command": "GET_STATUS"}\n')
    r1 = _read_line(a)
    r2 = _read_line(a)
    assert r1["status"] == r2["status"] == "success"
    assert len(server.received) >= 2
    a.close()


def test_disconnected_client_is_dropped(server):
    a = _client(server.bound_port)
    _wait_clients(server, 1)
    a.close()
    t0 = time.time()
    while server.client_count and time.time() - t0 < 5:
        time.sleep(0.05)
    assert server.client_count == 0
    # sending to a vanished client id is a harmless no-op
    assert server.send_response('{"status": "success"}', client_id=999) is False
