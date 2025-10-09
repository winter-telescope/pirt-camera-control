import os
import sys

# Add required DLL directories for BitFlow SDK and Camera Link Serial driver
os.add_dll_directory(r"C:\BitFlow SDK 6.5\Bin64")
os.add_dll_directory(r"C:\Program Files\CameraLink\Serial")

import json
import socket
import threading
import time
from datetime import datetime, timezone

import BFModule.BufferAcquisition as Buf
import BFModule.CLComm as CLCom
import numpy as np
from astropy.io import fits
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtNetwork import QHostAddress, QTcpServer
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class CommandServer(QThread):
    """TCP/IP server running in separate thread to handle remote commands"""

    command_received = pyqtSignal(str)

    def __init__(self, port=5555):
        super().__init__()
        self.port = port
        self.server = None
        self.running = False
        self.clients = []

    def run(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("0.0.0.0", self.port))
        self.server.listen(5)
        self.server.settimeout(1.0)  # Allow checking self.running periodically
        self.running = True

        print(f"Command server listening on port {self.port}")

        while self.running:
            try:
                client, addr = self.server.accept()
                print(f"Client connected from {addr}")
                client.settimeout(1.0)
                client_thread = threading.Thread(
                    target=self.handle_client, args=(client,)
                )
                client_thread.daemon = True
                client_thread.start()
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Server error: {e}")

    def handle_client(self, client):
        """Handle individual client connections"""
        self.clients.append(client)
        try:
            while self.running:
                try:
                    data = client.recv(1024)
                    if not data:
                        break

                    # Decode command
                    command = data.decode("utf-8").strip()
                    if command:
                        print(f"Received command: {command}")
                        self.command_received.emit(command)

                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"Client error: {e}")
                    break
        finally:
            self.clients.remove(client)
            client.close()

    def send_response(self, response):
        """Send response to all connected clients"""
        response_data = (response + "\n").encode("utf-8")
        for client in self.clients[
            :
        ]:  # Copy list to avoid modification during iteration
            try:
                client.send(response_data)
            except:
                self.clients.remove(client)

    def stop(self):
        self.running = False
        if self.server:
            self.server.close()
        for client in self.clients:
            client.close()


class ImageViewer(QWidget):
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PIRT Quick Look Viewer")
        self.setGeometry(750, 100, 600, 600)
        self.layout = QVBoxLayout()
        self.image_label = QLabel("No image yet")
        self.image_label.setAlignment(Qt.AlignCenter)
        image_hist_split = QVBoxLayout()
        image_hist_split.setContentsMargins(0, 0, 0, 0)
        image_hist_split.setSpacing(2)
        image_hist_split.addWidget(self.image_label, stretch=5)
        self.hist_label = QLabel()
        self.hist_label.setAlignment(Qt.AlignCenter)
        image_hist_split.addWidget(self.hist_label, stretch=1)
        self.layout.addLayout(image_hist_split)
        self.hist_label.setAlignment(Qt.AlignCenter)

        self.setLayout(self.layout)

    def update_image(self, data):
        median = np.median(data)
        std = np.std(data)
        lower = median - 3 * std
        upper = median + 3 * std
        clipped = np.clip(data, lower, upper)
        norm_data = 255 * (clipped - lower) / (upper - lower if upper > lower else 1)
        norm_data = norm_data.astype(np.uint8)

        from io import BytesIO

        import matplotlib.pyplot as plt

        plt.figure(figsize=(4, 2))
        plt.hist(
            data.ravel(),
            bins=256,
            color="gray",
            alpha=0.75,
            range=(median - 3 * std, median + 3 * std),
        )
        mean = np.mean(data)
        mode = np.bincount(data.ravel()).argmax() if data.size > 0 else 0
        plt.title(
            f"Histogram | Mean: {mean:.1f}, Median: {median:.1f}, Mode: {mode}, Std: {std:.1f}",
            fontsize=8,
        )
        plt.tight_layout()
        buf = BytesIO()
        plt.savefig(buf, format="png")
        buf.seek(0)
        plt.close()

        qimg_hist = QImage()
        qimg_hist.loadFromData(buf.read(), "PNG")
        self.hist_label.setPixmap(
            QPixmap.fromImage(qimg_hist).scaledToWidth(
                self.width(), Qt.SmoothTransformation
            )
        )
        h, w = norm_data.shape
        qimg = QImage(norm_data.data, w, h, w, QImage.Format_Grayscale8)
        self.image_label.setPixmap(
            QPixmap.fromImage(qimg).scaled(
                self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )


class SciCamGUI(QWidget):
    waiting_on_exposure_update = False

    def __init__(self, enable_server=True, server_port=5555):
        super().__init__()
        self.setWindowTitle("PIRT Control Panel")
        self.setGeometry(100, 100, 600, 500)
        self.setup_ui()
        self.setup_serial()

        self.exp_input.blockSignals(True)
        self.exp_input.setValue(1.0)  # GUI default only, no serial write
        self.exp_input.blockSignals(False)

        self.state = {}

        self.update_status_indicators()
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.update_status_indicators)
        self.status_timer.start(5000)

        # Disable capture button until TEC is locked
        self.capture_button.setEnabled(False)

        # Initialize command server
        self.command_server = None
        if enable_server:
            self.start_command_server(server_port)

        # Initialize capture settings
        self.custom_headers = {}
        self.save_as_stack = False
        self.custom_filename = None

    def start_command_server(self, port):
        """Start the TCP/IP command server"""
        self.command_server = CommandServer(port)
        self.command_server.command_received.connect(self.process_remote_command)
        self.command_server.start()
        self.print_terminal(f"TCP/IP command server started on port {port}")

    def process_remote_command(self, command):
        """Process commands received from TCP/IP clients"""
        try:
            # Parse JSON command
            cmd_data = json.loads(command)
            cmd_type = cmd_data.get("command", "").upper()

            response = {"status": "error", "message": "Unknown command"}

            if cmd_type == "CAPTURE":
                # Capture frame(s)
                nframes = cmd_data.get("nframes", 1)
                self.nframes_input.setText(str(nframes))

                # Store custom headers and stack mode for capture
                self.custom_headers = cmd_data.get("headers", {})
                self.save_as_stack = cmd_data.get("stack", False)
                self.custom_filename = cmd_data.get("filename", None)

                # Check if TEC is locked
                if not self.capture_button.isEnabled():
                    response = {"status": "error", "message": "TEC not locked"}
                else:
                    # Send immediate response before starting capture
                    response = {
                        "status": "success",
                        "message": f"Starting capture of {nframes} frame(s)",
                    }
                    if self.command_server:
                        self.command_server.send_response(json.dumps(response))

                    # Trigger capture (notifications will be sent as frames are saved)
                    self.capture_frame()
                    return  # Don't send response again at the end

            elif cmd_type == "SET_EXPOSURE":
                # Set exposure time
                exp_time = cmd_data.get("exposure", 1.0)
                wait_for_completion = cmd_data.get("wait", True)  # Default to waiting

                self.exp_input.setValue(exp_time)
                # Manually trigger the exposure change handler
                success = self.on_exposure_changed()

                if not success:
                    response = {
                        "status": "error",
                        "message": f"Failed to set exposure to {exp_time}s - value out of range",
                    }
                elif wait_for_completion and self.waiting_on_exposure_update:
                    # Wait synchronously for exposure to settle
                    wait_success = self.wait_for_exposure_update()
                    if wait_success:
                        response = {
                            "status": "success",
                            "message": f"Exposure set to {exp_time}s and settled",
                        }
                    else:
                        response = {
                            "status": "warning",
                            "message": f"Exposure set to {exp_time}s but wait timeout exceeded",
                        }
                    # Send response after waiting
                    if self.command_server:
                        self.print_terminal(
                            f"Sending response: {response['status']} - {response.get('message', '')}"
                        )
                        self.command_server.send_response(json.dumps(response))
                    return  # Don't send response again at the end
                else:
                    response = {
                        "status": "success",
                        "message": f"Exposure set to {exp_time}s (not waiting for settle)",
                    }

            elif cmd_type == "SET_TEC_TEMP":
                # Set TEC temperature
                temp = float(cmd_data.get("temperature", -40.0))
                min_allowed = -60.0
                max_allowed = 20.0
                if temp <= max_allowed and temp >= min_allowed:
                    # Block signals to prevent double command sending
                    self.tec_temp_dropdown.blockSignals(True)
                    self.tec_temp_dropdown.setCurrentText(str(temp))
                    self.tec_temp_dropdown.blockSignals(False)
                    # Manually trigger the TEC temperature change handler
                    self.handle_tec_temp_change(temp)
                    response = {
                        "status": "success",
                        "message": f"TEC temp set to {temp}°C",
                    }
                else:
                    response = {
                        "status": "error",
                        "message": f"Invalid temperature. Must be between {min_allowed}°C and {max_allowed}°C",
                    }

            elif cmd_type == "TEC_EN":
                # Enable or disable TEC
                value = cmd_data.get("value", "").upper()

                if value in ["ON", "OFF"]:
                    self.send_command(f"TEC:EN {value}")
                    response = {
                        "status": "success",
                        "message": f"TEC enabled set to {value}",
                    }

            elif cmd_type == "SET_OBJECT":
                # Set object name
                obj_name = cmd_data.get("object", "")
                self.object_input.setText(obj_name)
                response = {"status": "success", "message": f"Object set to {obj_name}"}

            elif cmd_type == "SET_OBSERVER":
                # Set observer name
                observer = cmd_data.get("observer", "")
                self.observer_input.setText(observer)
                response = {
                    "status": "success",
                    "message": f"Observer set to {observer}",
                }

            elif cmd_type == "SET_PATH":
                # Set save path
                path = cmd_data.get("path", "")
                if path:
                    # Expand user paths like ~/data
                    # path = os.path.expanduser(path)
                    # Create directory if it doesn't exist
                    try:
                        os.makedirs(path, exist_ok=True)
                        self.save_path_input.setText(path)
                        response = {
                            "status": "success",
                            "message": f"Save path set to {path}",
                        }
                    except Exception as e:
                        response = {
                            "status": "error",
                            "message": f"Failed to create directory: {str(e)}",
                        }
                else:
                    response = {"status": "error", "message": "No path provided"}

            elif cmd_type == "SET_FILENAME":
                # Set custom filename for next capture
                filename = cmd_data.get("filename", "")
                if filename:
                    self.custom_filename = filename
                    response = {
                        "status": "success",
                        "message": f"Next capture will use filename: {filename}",
                    }
                else:
                    self.custom_filename = None
                    response = {
                        "status": "success",
                        "message": "Filename reset to default naming",
                    }

            elif cmd_type == "GET_STATUS":
                # Get current status
                default = None
                status = {
                    "tec_locked": self.state.get("tec_lock", default),
                    "exposure": self.exp_input.value(),
                    "nframes": int(self.nframes_input.text()),
                    "object": self.object_input.text(),
                    "observer": self.observer_input.text(),
                    "save_path": self.save_path_input.text(),
                    "tec_temp": self.state.get("tec_temp", default),
                    "tec_setpoint": self.state.get("tec_setpoint", default),
                    "soc": self.state.get("soc", default),
                    "gain_corr": self.state.get("gain_corr", default),
                    "offset_corr": self.state.get("offset_corr", default),
                    "sub_corr": self.state.get("sub_corr", default),
                    "tec_lock": self.state.get("tec_lock", default),
                    "tec_enabled": self.state.get("tec_enabled", default),
                    "tec_voltage": self.state.get("tec_voltage", default),
                    "case_temp": self.state.get("case_temp", default),
                    "digpcb_temp": self.state.get("digpcb_temp", default),
                    "senspcb_temp": self.state.get("senspcb_temp", default),
                    "waiting_on_exposure_update": int(self.waiting_on_exposure_update),
                }
                response = {"status": "success", "data": status}

            elif cmd_type == "SET_CORRECTION":
                # Set correction parameters
                corr_type = cmd_data.get("type", "").upper()
                value = cmd_data.get("value", "").upper()

                if corr_type == "GAIN" and value in ["ON", "OFF"]:
                    # Block signals to prevent double command sending
                    self.gaincor_dropdown.blockSignals(True)
                    self.gaincor_dropdown.setCurrentText(value)
                    self.gaincor_dropdown.blockSignals(False)
                    # Manually send the command
                    self.send_command(f"CORR:GAIN {value}")
                    response = {
                        "status": "success",
                        "message": f"Gain correction set to {value}",
                    }
                elif corr_type == "OFFSET" and value in ["ON", "OFF"]:
                    self.offcor_dropdown.blockSignals(True)
                    self.offcor_dropdown.setCurrentText(value)
                    self.offcor_dropdown.blockSignals(False)
                    self.send_command(f"CORR:OFFSET {value}")
                    response = {
                        "status": "success",
                        "message": f"Offset correction set to {value}",
                    }
                elif corr_type == "SUB" and value in ["ON", "OFF"]:
                    self.subcor_dropdown.blockSignals(True)
                    self.subcor_dropdown.setCurrentText(value)
                    self.subcor_dropdown.blockSignals(False)
                    self.send_command(f"CORR:SUB {value}")
                    response = {
                        "status": "success",
                        "message": f"Substitution correction set to {value}",
                    }
                else:
                    response = {
                        "status": "error",
                        "message": "Invalid correction type or value",
                    }

            elif cmd_type == "SERIAL_COMMAND":
                # Send raw serial command
                serial_cmd = cmd_data.get("serial_command", "")
                if serial_cmd:
                    result = self.send_command(serial_cmd)
                    response = {"status": "success", "message": result}
                else:
                    response = {
                        "status": "error",
                        "message": "No serial command provided",
                    }

            # Send response back to client
            # mute this for GET_STATUS to reduce spam
            if cmd_type != "GET_STATUS":
                if self.command_server:
                    self.print_terminal(
                        f"Sending response: {response['status']} - {response.get('message', '')}"
                    )
                    self.command_server.send_response(json.dumps(response))

        except json.JSONDecodeError:
            response = {"status": "error", "message": "Invalid JSON command"}
            if self.command_server:
                self.command_server.send_response(json.dumps(response))
        except Exception as e:
            response = {"status": "error", "message": str(e)}
            if self.command_server:
                self.command_server.send_response(json.dumps(response))
            self.print_terminal(f"Error processing command: {e}")

    def update_status_indicators(self):
        setpoint = self.query_scalar("TEMP:SENS:SET?")

        if setpoint:
            try:
                setpoint = float(setpoint)
            except Exception:
                setpoint = -888.0
            self.tec_setpoint_label.setText(f"Setpoint (°C): {setpoint}")

        self.state.update({"tec_setpoint": setpoint})

        temp = self.query_scalar("TEMP:SENS?")
        if temp:
            try:
                temp = float(temp)
            except Exception:
                temp = -888.0
            self.dynamic_temp_label.setText(f"Temp (°C): {temp}")
        self.state.update({"tec_temp": temp})

        soc = self.query_scalar("SOC?")
        self.state.update({"soc": soc})
        if soc:
            self.soc_label.setText(f"Loaded SOC: {soc}")

        gain = self.query_scalar("CORR:GAIN?")
        if gain:
            self.gaincor_dropdown.setCurrentText(gain.strip().upper())
            if gain.lower() == "on":
                gain = 1
            elif gain.lower() == "off":
                gain = 0
            else:
                gain = None
        self.state.update({"gain_corr": gain})

        off = self.query_scalar("CORR:OFFSET?")
        if off:
            self.offcor_dropdown.setCurrentText(off.strip().upper())
            if off.lower() == "on":
                off = 1
            elif off.lower() == "off":
                off = 0
            else:
                off = None
        self.state.update({"offset_corr": off})

        sub = self.query_scalar("CORR:SUB?")
        if sub:
            self.subcor_dropdown.setCurrentText(sub.strip().upper())
            if sub.lower() == "on":
                sub = 1
            elif sub.lower() == "off":
                sub = 0
            else:
                sub = None
        self.state.update({"sub_corr": sub})

        tec_lock = self.query_scalar("TEC:LOCK?")

        if tec_lock and tec_lock.strip().upper() == "ON":
            self.tec_lock_light.setStyleSheet(
                "background-color: green; border-radius: 8px;"
            )
            self.capture_button.setEnabled(True)
            tec_lock_status = 1
        else:
            self.tec_lock_light.setStyleSheet(
                "background-color: red; border-radius: 8px;"
            )
            self.capture_button.setEnabled(False)
            tec_lock_status = 0
        self.state.update({"tec_lock": tec_lock_status})

        # TEC Voltage
        tec_voltage = self.query_scalar("TEC:V?")
        if tec_voltage:
            try:
                tec_voltage = float(tec_voltage)
            except Exception:
                tec_voltage = -888.0
        self.state.update({"tec_voltage": tec_voltage})

        # Case Temperature
        case_temp = self.query_scalar("TEMP:CASE?")
        if case_temp:
            try:
                case_temp = float(case_temp)
            except Exception:
                case_temp = -888.0
        self.state.update({"case_temp": case_temp})

        # DIGPCB Temperature
        digpcb_temp = self.query_scalar("TEMP:DIGPCB?")
        if digpcb_temp:
            try:
                digpcb_temp = float(digpcb_temp)
            except Exception:
                digpcb_temp = -888.0
        self.state.update({"digpcb_temp": digpcb_temp})

        # SENSPCB Temperature
        senspcb_temp = self.query_scalar("TEMP:SENSPCB?")
        if senspcb_temp:
            try:
                senspcb_temp = float(senspcb_temp)
            except Exception:
                senspcb_temp = -888.0
        self.state.update({"senspcb_temp": senspcb_temp})

        # TEC Status (enabled/disabled)
        tec_status = self.query_scalar("TEC:EN?")
        if tec_status and tec_status.strip().upper() == "ON":
            tec_enabled = 1
        elif tec_status and tec_status.strip().upper() == "OFF":
            tec_enabled = 0
        else:
            tec_enabled = None
        self.state.update({"tec_enabled": tec_enabled})

    def setup_serial(self):
        self.CL = CLCom.clsCLAllSerial()
        self.CL.SerialInit(0)
        self.CL.SetBaudRate(CLCom.BaudRates.CLBaudRate115200)
        time.sleep(0.2)

    def setup_ui(self):
        layout = QVBoxLayout()

        labels_layout = QVBoxLayout()
        labels_layout.setSpacing(2)

        self.soc_label = QLabel("Loaded SOC: Unknown")
        self.gaincor_dropdown = QComboBox()
        self.gaincor_dropdown.addItems(["OFF", "ON"])
        self.gaincor_dropdown.currentTextChanged.connect(
            lambda val: self.send_command(f"CORR:GAIN {val}")
        )

        self.offcor_dropdown = QComboBox()
        self.offcor_dropdown.addItems(["OFF", "ON"])
        self.offcor_dropdown.currentTextChanged.connect(
            lambda val: self.send_command(f"CORR:OFFSET {val}")
        )

        self.subcor_dropdown = QComboBox()
        self.subcor_dropdown.addItems(["OFF", "ON"])
        self.subcor_dropdown.currentTextChanged.connect(
            lambda val: self.send_command(f"CORR:SUB {val}")
        )

        self.tec_lock_light = QLabel()
        self.tec_lock_light.setFixedSize(16, 16)
        self.tec_lock_light.setStyleSheet("background-color: gray; border-radius: 8px;")
        tec_lock_row = QHBoxLayout()
        tec_lock_row.addWidget(QLabel("TEC Lock:"))
        tec_lock_row.addWidget(self.tec_lock_light)

        labels_layout.addWidget(self.soc_label)
        labels_layout.addWidget(QLabel("Gain Corr:"))
        labels_layout.addWidget(self.gaincor_dropdown)
        labels_layout.addWidget(QLabel("Offset Corr:"))
        labels_layout.addWidget(self.offcor_dropdown)
        labels_layout.addWidget(QLabel("Subst Corr:"))
        labels_layout.addWidget(self.subcor_dropdown)
        labels_layout.addLayout(tec_lock_row)
        self.dynamic_temp_label = QLabel("Temp (°C): Unknown")
        labels_layout.addWidget(self.dynamic_temp_label)

        self.tec_setpoint_label = QLabel("Setpoint (°C): Unknown")
        labels_layout.addWidget(self.tec_setpoint_label)

        self.tec_temp_dropdown = QComboBox()
        self.tec_temp_dropdown.addItems(["-20", "-40", "-60"])
        self.tec_temp_dropdown.setCurrentText("-40")
        self.tec_temp_dropdown.currentTextChanged.connect(self.handle_tec_temp_change)
        tec_temp_row = QHBoxLayout()
        tec_temp_row.addWidget(QLabel("Set TEC Temp:"))
        tec_temp_row.addWidget(self.tec_temp_dropdown)
        labels_layout.addLayout(tec_temp_row)

        header_layout = QHBoxLayout()
        header_layout.addLayout(labels_layout)
        layout.addLayout(header_layout)

        self.exp_label = QLabel("Exposure Time (s):")
        layout.addWidget(self.exp_label)

        self.exp_input = QDoubleSpinBox()
        self.exp_input.setDecimals(6)
        self.exp_input.setMaximum(999.999999)
        self.exp_input.setRange(0.001, 999.999999)
        self.exp_input.setSingleStep(0.01)

        self.nframes_label = QLabel("# Frames:")
        self.nframes_input = QLineEdit()
        self.nframes_input.setText("1")
        self.nframes_input.setMaximumWidth(50)

        exp_row = QHBoxLayout()
        exp_row.addWidget(self.exp_input)
        exp_row.addWidget(self.nframes_label)
        exp_row.addWidget(self.nframes_input)
        layout.addLayout(exp_row)

        QTimer.singleShot(
            0, lambda: self.exp_input.editingFinished.connect(self.on_exposure_changed)
        )

        form_row = QHBoxLayout()
        self.object_label = QLabel("Object:")
        self.object_input = QLineEdit()
        self.object_input.setText("SCICAM")
        self.observer_label = QLabel("Observer:")
        self.observer_input = QLineEdit()
        self.observer_input.setText("MDM")
        form_row.addWidget(self.object_label)
        form_row.addWidget(self.object_input)
        form_row.addWidget(self.observer_label)
        form_row.addWidget(self.observer_input)
        layout.addLayout(form_row)

        self.file_label = QLabel("Save Folder:")
        layout.addWidget(self.file_label)

        path_layout = QHBoxLayout()
        self.save_path_input = QLineEdit()
        # Platform-independent home directory path
        default_folder = os.path.expanduser("~/data")
        # Create directory if it doesn't exist
        os.makedirs(default_folder, exist_ok=True)
        self.save_path_input.setText(default_folder)
        path_layout.addWidget(self.save_path_input)

        self.browse_button = QPushButton("Browse")
        self.browse_button.clicked.connect(self.browse_folder)
        path_layout.addWidget(self.browse_button)
        layout.addLayout(path_layout)

        self.capture_button = QPushButton("Capture Frame")
        self.capture_button.clicked.connect(self.capture_frame)
        layout.addWidget(self.capture_button)

        self.status_label = QLabel("Status: Idle")
        layout.addWidget(self.status_label)

        self.terminal_output = QTextEdit()
        self.terminal_output.setReadOnly(True)
        layout.addWidget(self.terminal_output)

        self.setLayout(layout)

    def browse_folder(self):
        # Use the current save path as starting directory, or ~/data if empty
        current_path = self.save_path_input.text().strip()
        if not current_path or not os.path.exists(current_path):
            current_path = os.path.expanduser("~/data")

        folder = QFileDialog.getExistingDirectory(
            self, "Select Save Folder", current_path
        )
        if folder:
            self.save_path_input.setText(folder)

    def append_output(self, text):
        self.terminal_output.append(text)
        self.terminal_output.ensureCursorVisible()

    def print_terminal(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"[{timestamp}] {message}"
        print(message)
        self.append_output(message)

    def send_command(self, command):
        self.print_terminal(f"Sending: {command}")
        self.CL.SerialWrite(command + "\r", 100)
        time.sleep(0.05)
        output = ""
        t0 = time.time()
        while time.time() - t0 < 2.0:
            chunk = self.CL.SerialRead(1, 256)
            if chunk:
                output += chunk.decode("utf-8", errors="ignore")
                if output.count("OK") >= 1 and ">" in output:
                    break
            else:
                time.sleep(0.05)

        # Log response if it contains useful info
        if output and not output.strip().endswith("OK\r\n>"):
            self.print_terminal(f"Response: {output.strip()}")

        return output

    def set_exposure(self, exposure_s):
        """Set exposure time and frame period. Returns True on success, False on error."""
        # Get the previous exposure time
        previous_exposure_str = self.query_scalar("SENS:EXPPER?")
        try:
            previous_exposure_cycles = (
                int(previous_exposure_str) if previous_exposure_str else 0
            )
        except ValueError:
            self.print_terminal(
                f"Warning: Could not parse previous exposure cycles: {previous_exposure_str}"
            )
            previous_exposure_cycles = 0

        CLOCK_FREQ = 15.0

        # Calculate new exposure and frame period cycles
        exposure_cycles = int((exposure_s * 1e6 * 1000) / (1e3 / CLOCK_FREQ))
        exposure_cycles = max(12, min(exposure_cycles, 4294967142))

        frame_cycles = int(((exposure_s + 0.1) * 1e6 * 1000) / (1e3 / CLOCK_FREQ))
        frame_cycles = max(1800, min(frame_cycles, 4294967295))

        # The order matters!
        # If we are increasing exposure, we need to set the frame period first
        # If we are decreasing exposure, we need to set the exposure period first
        if exposure_cycles > previous_exposure_cycles:
            self.print_terminal(
                f"Exposure increasing: {previous_exposure_cycles} → {exposure_cycles} cycles"
            )
            # Set frame period first
            result = self.send_command(f"SENS:FRAMEPER {frame_cycles}")
            if "Out of Range" in result or "ERROR" in result:
                self.print_terminal(
                    f"ERROR: Frame period out of range. Cycles: {frame_cycles}"
                )
                return False

            # Then set exposure
            result = self.send_command(f"SENS:EXPPER {exposure_cycles}")
            if "Out of Range" in result or "ERROR" in result:
                self.print_terminal(
                    f"ERROR: Exposure value out of range. Cycles: {exposure_cycles}"
                )
                # Try to restore frame period to match previous exposure
                prev_frame_cycles = int(
                    ((previous_exposure_cycles / (CLOCK_FREQ * 1e6)) + 0.1)
                    * 1e6
                    * 1000
                    / (1e3 / CLOCK_FREQ)
                )
                self.send_command(f"SENS:FRAMEPER {prev_frame_cycles}")
                return False
        else:
            self.print_terminal(
                f"Exposure decreasing: {previous_exposure_cycles} → {exposure_cycles} cycles"
            )
            # Set exposure first
            result = self.send_command(f"SENS:EXPPER {exposure_cycles}")
            if "Out of Range" in result or "ERROR" in result:
                self.print_terminal(
                    f"ERROR: Exposure value out of range. Cycles: {exposure_cycles}"
                )
                return False

            # Then set frame period
            result = self.send_command(f"SENS:FRAMEPER {frame_cycles}")
            if "Out of Range" in result or "ERROR" in result:
                self.print_terminal(
                    f"ERROR: Frame period out of range. Cycles: {frame_cycles}"
                )
                # Try to restore exposure to previous value
                self.send_command(f"SENS:EXPPER {previous_exposure_cycles}")
                return False

        return True

    def on_exposure_changed(self):
        """Handle exposure time changes from GUI"""
        self.waiting_on_exposure_update = True
        self.exposure_update_start_time = time.time()
        new_exp = self.exp_input.value()

        # Use set_exposure method for consistency
        if not self.set_exposure(new_exp):
            self.waiting_on_exposure_update = False
            self.capture_button.setEnabled(True)
            return False

        # Calculate wait time based on frame period
        CLOCK_FREQ = 15.0
        frame_cycles = int(((new_exp + 0.1) * 1e6 * 1000) / (1e3 / CLOCK_FREQ))
        frame_cycles = max(1800, min(frame_cycles, 4294967295))

        wait_time_sec = (3 * frame_cycles) / (CLOCK_FREQ * 1e6)
        self.exposure_wait_time = wait_time_sec
        self.exposure_wait_end_time = time.time() + wait_time_sec
        self.capture_button.setEnabled(False)
        self.print_terminal(
            f"Exposure time changed to {new_exp}s — delaying for {wait_time_sec:.1f} sec"
        )
        QTimer.singleShot(int(wait_time_sec * 1000), self.enable_capture_button)
        return True

    def handle_tec_temp_change(self, target):
        try:
            target = float(target)
            set_cmd = self.query_scalar("TEMP:SENS:SET?")
            current = float(set_cmd) if set_cmd else None
            if current is None:
                self.print_terminal("Could not read current TEC setpoint.")
                return

            self.send_command(f"TEMP:SENS:SET {target}")
            """
            # Old warmup procedure (now disabled)
            # which enforces slow ramp.
            # The idea is good but the implementation is bad and blocking.
            # TODO: Re-implement with non-blocking approach
            
            if abs(current + 60.0) < 1.0 and target > -60:
                for step in [-55, -50, -45]:
                    if step > target:
                        break
                    self.print_terminal(f"Warming up: setting TEC to {step}°C")
                    self.send_command(f"TEMP:SENS:SET {step}")
                    self.wait_for_tec_lock(timeout_sec=300)
            self.print_terminal(f"Final TEC setpoint: {target}°C")
            self.send_command(f"TEMP:SENS:SET {target}")
            """
        except Exception as e:
            self.print_terminal(f"Error during TEC warm-up: {e}")

    def wait_for_tec_lock(self, timeout_sec=300):
        self.print_terminal("Waiting for TEC to lock...")
        start = time.time()
        time.sleep(15)
        while time.time() - start < timeout_sec:
            temp = self.query_scalar("TEMP:SENS?")
            if temp:
                self.print_terminal(f"Current sensor temp: {temp} °C")
            lock = self.query_scalar("TEC:LOCK?")
            if lock and lock.strip().upper() == "ON":
                self.print_terminal("TEC locked. Waiting for minimum duration...")
            time.sleep(2)
        self.print_terminal("WARNING: TEC failed to lock within timeout.")

    def query_scalar(self, command):
        try:
            self.CL.SerialWrite(command + "\r", 100)
            time.sleep(0.05)
            output = ""
            t0 = time.time()
            while time.time() - t0 < 2.0:
                chunk = self.CL.SerialRead(1, 256)
                if chunk:
                    output += chunk.decode("utf-8", errors="ignore")
                    if "OK" in output and ">" in output:
                        break
                else:
                    time.sleep(0.05)
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            values = [
                line
                for line in lines
                if not line.startswith((">", command.split(":")[0], "ERROR"))
                and line not in ("OK",)
            ]
            if values:
                return values[-1]
        except Exception as e:
            self.print_terminal(f"Error querying {command}: {e}")
        return None

    def enable_capture_button(self):
        """Re-enable capture button after exposure update completes"""
        self.capture_button.setEnabled(True)
        self.waiting_on_exposure_update = False
        self.print_terminal("Exposure update complete - ready for capture")

    def wait_for_exposure_update(self):
        """Synchronously wait for exposure update to complete"""
        if not self.waiting_on_exposure_update:
            self.print_terminal("No exposure update in progress")
            return True

        # Calculate remaining wait time
        if hasattr(self, "exposure_wait_end_time"):
            remaining_time = self.exposure_wait_end_time - time.time()
            if remaining_time > 0:
                self.print_terminal(
                    f"Waiting {remaining_time:.1f}s for exposure to settle..."
                )

                # Wait while processing events to keep GUI responsive
                start_wait = time.time()
                while (
                    time.time() < self.exposure_wait_end_time
                    and self.waiting_on_exposure_update
                ):
                    QApplication.processEvents()
                    time.sleep(0.01)

                    # Safety timeout
                    if time.time() - start_wait > 120:  # 2 minute max wait
                        self.print_terminal("WARNING: Exposure wait timeout exceeded")
                        self.waiting_on_exposure_update = False
                        self.capture_button.setEnabled(True)
                        return False

                # Wait completed successfully
                self.waiting_on_exposure_update = False
                self.capture_button.setEnabled(True)
                self.print_terminal("Exposure settling complete")
                return True
            else:
                # Time already passed
                self.waiting_on_exposure_update = False
                self.capture_button.setEnabled(True)
                return True
        else:
            # Fallback if end time not set
            self.print_terminal("WARNING: Exposure wait time not properly set")
            time.sleep(5)  # Default wait
            self.waiting_on_exposure_update = False
            self.capture_button.setEnabled(True)
            return True

    def capture_frame(self):
        # Wait for exposure update if one is in progress
        if self.waiting_on_exposure_update:
            self.print_terminal("Waiting for exposure update to complete...")

            # Use the calculated wait time if available, otherwise default to 60 seconds
            wait_timeout = (
                getattr(self, "exposure_wait_time", 60.0) + 5.0
            )  # Add 5s buffer
            start_time = time.time()

            while (
                self.waiting_on_exposure_update
                and (time.time() - start_time) < wait_timeout
            ):
                QApplication.processEvents()  # Keep GUI responsive
                time.sleep(0.1)

            if self.waiting_on_exposure_update:
                self.print_terminal(
                    f"ERROR: Exposure update timeout exceeded ({wait_timeout:.1f}s)"
                )
                # Force reset the flag to avoid permanent blocking
                self.waiting_on_exposure_update = False
                self.capture_button.setEnabled(True)
                return
            else:
                self.print_terminal("Exposure update complete, proceeding with capture")

        try:
            nframes = int(self.nframes_input.text().strip())
        except ValueError:
            self.print_terminal("Invalid number of frames; defaulting to 1")
            nframes = 1

        self.CL.SerialClose()

        # Initialize list for stacking if needed
        image_stack = []
        stack_headers = []
        stack_filename = None

        for i in range(nframes):
            self.status_label.setText(f"Status: Waiting for frame {i+1}/{nframes}...")

            CirAq = Buf.clsCircularAcquisition(Buf.ErrorMode.ErIgnore)
            CirAq.Open(0)
            numbuffers = 2
            BufArr = CirAq.BufferSetup(numbuffers)
            CirAq.AqSetup(Buf.SetupOptions.setupDefault)
            CirAq.AqControl(Buf.AcqCommands.Start, Buf.AcqControlOptions.Wait)

            framearr = False
            t0 = time.time()
            self.print_terminal(f"Starting recording {i+1}/{nframes} ..")

            while not framearr:
                try:
                    curBuf = CirAq.WaitForFrame(1000)
                except Buf.PythonMemException:
                    self.print_terminal("Waiting for frame arrival")
                    CirAq.AqCleanup()
                    CirAq.BufferCleanup()
                    CirAq.Close()

                    CirAq = Buf.clsCircularAcquisition(Buf.ErrorMode.ErIgnore)
                    CirAq.Open(0)
                    BufArr = CirAq.BufferSetup(numbuffers)
                    CirAq.AqSetup(Buf.SetupOptions.setupDefault)
                    CirAq.AqControl(Buf.AcqCommands.Start, Buf.AcqControlOptions.Wait)
                    continue
                else:
                    framearr = True
                    bufnum = curBuf.BufferNumber
                    t1 = time.time()

            total_time = t1 - t0
            self.print_terminal(f"Total acquisition time: {total_time:.2f} seconds")

            img = np.copy(np.asarray(BufArr[bufnum], dtype=np.uint16))
            CirAq.AqCleanup()
            CirAq.BufferCleanup()
            CirAq.Close()

            now = datetime.now(timezone.utc).replace(microsecond=0)
            curtime = now.strftime("%Y%m%dT%H%M%S")
            folder = self.save_path_input.text().strip()
            os.makedirs(folder, exist_ok=True)

            # Create header
            hdr = fits.Header()
            mjd = now.timestamp() / 86400.0 + 40587  # Convert Unix time to MJD
            hdr["MJD-OBS"] = (mjd, "Modified Julian Date of observation")

            object_name = self.object_input.text().strip()
            observer_name = self.observer_input.text().strip()
            if object_name:
                hdr["OBJECT"] = (object_name, "Object name")
            if observer_name:
                hdr["OBSERVER"] = (observer_name, "Observer name")

            CLOCK_FREQ_MHZ = 15.0
            queries = {
                "EXPTIME": ("SENS:EXPPER?", "Exposure time (s)"),
                "FRMTIME": ("SENS:FRAMEPER?", "Frame period (s)"),
                "CLKFREQ": ("SENS:CLOCKFREQ?", "Clock frequency"),
                "XSIZE": ("SENS:XSIZE?", "Horizontal ROI size"),
                "YSIZE": ("SENS:YSIZE?", "Vertical ROI size"),
                "XSTART": ("SENS:XSTART?", "Horizontal ROI start"),
                "YSTART": ("SENS:YSTART?", "Vertical ROI start"),
                "TMP_SET": ("TEMP:SENS:SET?", "Sensor temp setpoint"),
                "TMP_CUR": ("TEMP:SENS?", "Sensor temp (C)"),
                "TEC_EN": ("TEC:EN?", "TEC enabled"),
                "TEC_LOCK": ("TEC:LOCK?", "TEC locked"),
                "FORMAT": ("DATA:FORMAT?", "Data format"),
                "GAINCOR": ("CORR:GAIN?", "Gain corr. enabled"),
                "OFFCOR": ("CORR:OFFSET?", "Offset corr. enabled"),
                "SUBCOR": ("CORR:SUB?", "Pixel subst. enabled"),
                "SOCNAME": ("SOC?", "Current SOC"),
                "MODEL": ("SYS:MODEL?", "Model"),
                "SERIAL": ("SYS:SN?", "Serial number"),
                "FWVERS": ("SYS:FW?", "Firmware version"),
                "SWVERS": ("SYS:SW?", "Software version"),
            }

            self.setup_serial()
            time.sleep(0.2)
            for key, (cmd, comment) in queries.items():
                val = self.query_scalar(cmd)
                if val is not None and not val.startswith(cmd):
                    try:
                        if key in ["EXPTIME", "FRMTIME"] and val.isdigit():
                            val = int(val)
                            val = val / (CLOCK_FREQ_MHZ * 1e6)
                        elif key == "CLKFREQ":
                            val = float(val.strip("MHZmhz")) * 1e6
                        elif key in ["XSIZE", "YSIZE", "XSTART", "YSTART"]:
                            val = int(val)
                        elif key in ["TMP_CUR", "TMP_SET"]:
                            val = float(val)
                        elif key in [
                            "TEC_EN",
                            "TEC_LOCK",
                            "GAINCOR",
                            "OFFCOR",
                            "SUBCOR",
                        ]:
                            val = 1 if val.upper() == "ON" else 0
                        else:
                            val = val.strip()
                        hdr[key] = (val, comment)
                    except Exception as e:
                        self.print_terminal(
                            f"Error converting {key} with value '{val}': {e}"
                        )

            # Add custom headers
            if hasattr(self, "custom_headers") and self.custom_headers:
                self._add_custom_headers(hdr, self.custom_headers)

            # Add frame-specific info for stacks
            if self.save_as_stack:
                hdr["FRAME"] = (i + 1, "Frame number in stack")
                hdr["DATEOBS"] = (now.isoformat(), "Date-time of this frame")

            self.CL.SerialClose()

            # Save based on mode
            if self.save_as_stack:
                # Store for stack
                image_stack.append(img)
                stack_headers.append(hdr)
                if i == 0:
                    # Allow custom filename or use default
                    if hasattr(self, "custom_filename") and self.custom_filename:
                        base_filename = self.custom_filename
                        if not base_filename.endswith(".fits"):
                            base_filename += ".fits"
                        stack_filename = os.path.join(folder, base_filename)
                    else:
                        stack_filename = os.path.join(
                            folder, "scicam_stack_" + curtime + ".fits"
                        )
                self.status_label.setText(f"Collected frame {i+1}/{nframes} for stack")

                # Update viewer for each frame in stack mode
                if hasattr(self, "viewer") and self.viewer:
                    self.viewer.update_image(img)
                    QApplication.processEvents()  # Force GUI update
            else:
                # Save individual file
                if (
                    hasattr(self, "custom_filename")
                    and self.custom_filename
                    and nframes == 1
                ):
                    # Use custom filename for single frame
                    base_filename = self.custom_filename
                    if not base_filename.endswith(".fits"):
                        base_filename += ".fits"
                    filename = os.path.join(folder, base_filename)
                else:
                    # Use default naming for multiple frames or no custom name
                    filename = os.path.join(folder, "scicam_" + curtime + ".fits")

                hdu = fits.PrimaryHDU(img, header=hdr)
                hdu.writeto(filename, overwrite=True)
                self.status_label.setText(f"Saved: {filename}")

                # Send notification to TCP clients if connected
                if self.command_server:
                    notification = {
                        "event": "frame_saved",
                        "filename": filename,
                        "frame": i + 1,
                        "total_frames": nframes,
                    }
                    self.command_server.send_response(json.dumps(notification))

                # Update viewer for individual frames
                if hasattr(self, "viewer") and self.viewer:
                    self.viewer.update_image(img)

            if i == nframes - 1:
                self.setup_serial()
                time.sleep(0.2)

        # If stacking, save the stack now
        if self.save_as_stack and image_stack:
            self.print_terminal(f"Saving stack of {len(image_stack)} frames...")

            # Create 3D array (frames, y, x)
            data_cube = np.array(image_stack, dtype=np.uint16)

            # Use first header as primary, add stack-specific keywords
            primary_hdr = stack_headers[0].copy()
            primary_hdr["NAXIS"] = 3
            primary_hdr["NAXIS3"] = len(image_stack)
            primary_hdr["NFRAMES"] = (len(image_stack), "Number of frames in stack")
            primary_hdr["STACKTYP"] = ("TEMPORAL", "Type of stack")

            # Create primary HDU with data cube
            primary_hdu = fits.PrimaryHDU(data_cube, header=primary_hdr)

            # Create HDU list
            hdul = fits.HDUList([primary_hdu])

            # Optionally add individual frame headers as extensions
            for idx, hdr in enumerate(stack_headers):
                # Create a table with frame metadata
                col1 = fits.Column(name="FRAME", format="I", array=[idx + 1])
                col2 = fits.Column(name="MJD_OBS", format="D", array=[hdr["MJD-OBS"]])
                cols = fits.ColDefs([col1, col2])
                tbhdu = fits.BinTableHDU.from_columns(cols)
                tbhdu.header["EXTNAME"] = f"FRAME{idx+1}"
                for key in ["MJD-OBS", "TMP_CUR", "DATEOBS"]:
                    if key in hdr:
                        tbhdu.header[key] = hdr[key]
                hdul.append(tbhdu)

            # Save the stack
            hdul.writeto(stack_filename, overwrite=True)
            hdul.close()

            self.status_label.setText(f"Saved stack: {stack_filename}")
            self.print_terminal(f"Stack saved: {stack_filename}")

            # Send notification for stack
            if self.command_server:
                notification = {
                    "event": "stack_saved",
                    "filename": stack_filename,
                    "frames": len(image_stack),
                    "total_frames": nframes,
                }
                self.command_server.send_response(json.dumps(notification))

        # Reset capture settings
        self.custom_headers = {}
        self.save_as_stack = False
        self.custom_filename = None

    def _add_custom_headers(self, hdr, headers_input):
        """Add custom headers from various input formats"""
        try:
            # If it's a dictionary
            if isinstance(headers_input, dict):
                for key, value in headers_input.items():
                    if isinstance(value, (list, tuple)) and len(value) >= 2:
                        # (key, value, comment) format
                        hdr[key.upper()] = (
                            value[0],
                            value[1] if len(value) > 1 else "",
                        )
                    else:
                        hdr[key.upper()] = value

            # If it's a list of tuples or Card objects
            elif isinstance(headers_input, list):
                for item in headers_input:
                    if isinstance(item, (list, tuple)):
                        if len(item) >= 2:
                            key = str(item[0]).upper()
                            value = item[1]
                            comment = item[2] if len(item) > 2 else ""
                            hdr[key] = (value, comment)
                    elif hasattr(item, "keyword") and hasattr(item, "value"):
                        # Astropy Card object
                        hdr[item.keyword] = (
                            item.value,
                            item.comment if hasattr(item, "comment") else "",
                        )

        except Exception as e:
            self.print_terminal(f"Error adding custom headers: {e}")

    def closeEvent(self, event):
        """Clean up when window is closed"""
        if self.command_server:
            self.command_server.stop()
            self.command_server.wait()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    viewer = ImageViewer()
    viewer.show()

    # You can disable the server by setting enable_server=False
    # or change the port with server_port=XXXX
    window = SciCamGUI(enable_server=True, server_port=5555)
    window.viewer = viewer
    window.show()
    sys.exit(app.exec_())
