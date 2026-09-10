"""
TCP/IP JSON command server for the PIRT camera GUI.

Protocol (unchanged from the original in-GUI server):
    * clients connect on ``port`` and send newline-delimited JSON commands
      such as ``{"command": "GET_STATUS"}``;
    * the GUI answers with a newline-delimited JSON dict;
    * the GUI also pushes unsolicited ``{"event": ...}`` notifications
      (frame saved, capture complete, ...) to every connected client.

What changed versus the original: replies are routed back to the client
that sent the command instead of being broadcast to everyone. With several
clients on the socket at once (the WSP spring camera daemon plus the GUI
watchdog's health probe) broadcast replies could be mistaken for the answer
to somebody else's command. Notifications are still broadcast on purpose.

This module has no BitFlow / camera dependencies so it can be unit tested
off-instrument.
"""

import itertools
import json
import socket
import threading

from PyQt5.QtCore import QThread, pyqtSignal


class CommandServer(QThread):
    """TCP/IP server running in a separate thread to handle remote commands.

    Signals:
        command_received(str, object): raw JSON command line and the id of
            the client that sent it. Pass the id back to :meth:`send_response`
            so the reply reaches only that client.
    """

    command_received = pyqtSignal(str, object)

    def __init__(self, port=5555, host="0.0.0.0", quiet_commands=("GET_STATUS",)):
        super().__init__()
        self.host = host
        self.port = port
        self.server = None
        self.running = False
        # Commands that are polled constantly and should not be echoed to the
        # console (upper-cased command names).
        self.quiet_commands = tuple(c.upper() for c in quiet_commands)

        # client_id -> socket. Guarded by _clients_lock because the accept
        # thread, per-client threads and the GUI thread all touch it.
        self._clients = {}
        self._clients_lock = threading.Lock()
        self._next_client_id = itertools.count(1)

        # Set once the listening socket is bound (port 0 => OS picks a port).
        self.bound_port = None
        self.ready = threading.Event()

    # ------------------------------------------------------------------
    # compatibility shim: the old server exposed ``clients`` as a list
    @property
    def clients(self):
        with self._clients_lock:
            return list(self._clients.values())

    @property
    def client_count(self):
        with self._clients_lock:
            return len(self._clients)

    # ------------------------------------------------------------------
    def run(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.port))
        self.server.listen(5)
        self.server.settimeout(1.0)  # Allow checking self.running periodically
        self.running = True
        self.bound_port = self.server.getsockname()[1]
        self.ready.set()

        print(f"Command server listening on port {self.bound_port}")

        while self.running:
            try:
                client, addr = self.server.accept()
                client_id = next(self._next_client_id)
                print(f"Client {client_id} connected from {addr}")
                client.settimeout(1.0)
                with self._clients_lock:
                    self._clients[client_id] = client
                client_thread = threading.Thread(
                    target=self.handle_client,
                    args=(client, client_id),
                    name=f"cmdserver-client-{client_id}",
                )
                client_thread.daemon = True
                client_thread.start()
            except socket.timeout:
                continue
            except OSError:
                # server socket closed by stop()
                if self.running:
                    print("Server socket error")
                break
            except Exception as e:
                print(f"Server error: {e}")

    def handle_client(self, client, client_id):
        """Handle one client connection until it disconnects."""
        buffer = ""  # Buffer to accumulate data

        try:
            while self.running:
                try:
                    data = client.recv(4096)
                    if not data:
                        break

                    # Decode and add to buffer
                    buffer += data.decode("utf-8", errors="replace")

                    # Process complete messages (newline-delimited)
                    while True:
                        newline_index = buffer.find("\n")
                        if newline_index == -1:
                            break  # No complete message yet

                        command = buffer[:newline_index]
                        buffer = buffer[newline_index + 1 :]

                        command = command.strip()
                        if not command:
                            continue

                        if not self._is_quiet(command):
                            print(
                                f"Received command from client {client_id}: "
                                f"{command[:100]}..."
                            )
                        self.command_received.emit(command, client_id)

                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"Client {client_id} error: {e}")
                    break
        finally:
            self._drop_client(client_id)
            print(f"Client {client_id} disconnected")

    def _is_quiet(self, command):
        try:
            cmd_data = json.loads(command)
            return cmd_data.get("command", "").upper() in self.quiet_commands
        except Exception:
            return False

    def _drop_client(self, client_id):
        with self._clients_lock:
            client = self._clients.pop(client_id, None)
        if client is not None:
            try:
                client.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    @staticmethod
    def _encode(message):
        if not isinstance(message, str):
            message = json.dumps(message)
        return (message.rstrip("\n") + "\n").encode("utf-8")

    def send_response(self, response, client_id=None):
        """Send a reply.

        Args:
            response: JSON string (or dict) to send.
            client_id: the id delivered with ``command_received``. If given
                and that client is still connected, only it receives the
                reply. If ``None`` the reply is broadcast (legacy behavior).

        Returns:
            True if the message was delivered to at least one client.
        """
        if client_id is None:
            return self.broadcast(response)

        payload = self._encode(response)
        with self._clients_lock:
            client = self._clients.get(client_id)
        if client is None:
            # Client went away before we could answer; nothing to do.
            return False
        try:
            client.sendall(payload)
            return True
        except OSError:
            self._drop_client(client_id)
            return False

    def broadcast(self, message):
        """Send a message to every connected client (used for notifications)."""
        payload = self._encode(message)
        with self._clients_lock:
            targets = list(self._clients.items())
        delivered = False
        for client_id, client in targets:
            try:
                client.sendall(payload)
                delivered = True
            except OSError:
                self._drop_client(client_id)
        return delivered

    def stop(self):
        self.running = False
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
        with self._clients_lock:
            clients = list(self._clients.items())
            self._clients.clear()
        for _, client in clients:
            try:
                client.close()
            except OSError:
                pass
