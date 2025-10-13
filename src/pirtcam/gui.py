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
from enum import Enum, auto
from pathlib import Path

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
    QProgressBar,
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
        buffer = ""  # Buffer to accumulate data

        try:
            while self.running:
                try:
                    data = client.recv(4096)
                    if not data:
                        break

                    # Decode and add to buffer
                    buffer += data.decode("utf-8")

                    # Process complete messages (newline-delimited)
                    while True:
                        # Look for newline delimiter
                        newline_index = buffer.find("\n")
                        if newline_index == -1:
                            # No complete message yet
                            break

                        # Extract the complete message
                        command = buffer[:newline_index]
                        buffer = buffer[newline_index + 1 :]  # Keep remainder in buffer

                        command = command.strip()
                        if command:
                            print(
                                f"Received command: {command[:100]}..."
                            )  # Print first 100 chars
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


class CaptureThread(QThread):
    """Thread for capturing frames without blocking the GUI"""

    status_update = pyqtSignal(str)
    frame_captured = pyqtSignal(int, int)  # current, total
    capture_complete = pyqtSignal()
    capture_error = pyqtSignal(str)
    image_ready = pyqtSignal(np.ndarray)
    notification = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.nframes = 1
        self.save_as_stack = False
        self.custom_headers = {}
        self.custom_filename = None

    def setup_capture(
        self, nframes, save_as_stack=False, custom_headers=None, custom_filename=None
    ):
        """Setup capture parameters before running thread"""
        self.nframes = nframes
        self.save_as_stack = save_as_stack
        self.custom_headers = custom_headers or {}
        self.custom_filename = custom_filename

    def run(self):
        """Run capture in separate thread"""
        try:
            # Initialize capture state in parent
            self.parent.is_capturing = True
            self.parent.capture_start_time = time.time()
            self.parent.total_frames = self.nframes
            self.parent.current_frame = 0
            self.parent.current_exposure_time = self.parent.exp_input.value()

            self.parent.CL.SerialClose()

            # Initialize list for stacking if needed
            image_stack = []
            stack_headers = []
            stack_filename = None

            for i in range(self.nframes):
                self.parent.current_frame = i + 1
                self.parent.frame_start_time = time.time()

                self.status_update.emit(
                    f"Status: Waiting for frame {i+1}/{self.nframes}..."
                )
                self.frame_captured.emit(i + 1, self.nframes)

                # Capture logic
                CirAq = Buf.clsCircularAcquisition(Buf.ErrorMode.ErIgnore)
                CirAq.Open(0)
                numbuffers = 2
                BufArr = CirAq.BufferSetup(numbuffers)
                CirAq.AqSetup(Buf.SetupOptions.setupDefault)
                CirAq.AqControl(Buf.AcqCommands.Start, Buf.AcqControlOptions.Wait)

                framearr = False
                t0 = time.time()
                self.parent.print_terminal(
                    f"Starting recording {i+1}/{self.nframes} .."
                )

                while not framearr:
                    try:
                        curBuf = CirAq.WaitForFrame(1000)
                    except Buf.PythonMemException:
                        self.parent.print_terminal("Waiting for frame arrival")
                        CirAq.AqCleanup()
                        CirAq.BufferCleanup()
                        CirAq.Close()

                        CirAq = Buf.clsCircularAcquisition(Buf.ErrorMode.ErIgnore)
                        CirAq.Open(0)
                        BufArr = CirAq.BufferSetup(numbuffers)
                        CirAq.AqSetup(Buf.SetupOptions.setupDefault)
                        CirAq.AqControl(
                            Buf.AcqCommands.Start, Buf.AcqControlOptions.Wait
                        )
                        continue
                    else:
                        framearr = True
                        bufnum = curBuf.BufferNumber
                        t1 = time.time()

                total_time = t1 - t0
                self.parent.print_terminal(
                    f"Total acquisition time: {total_time:.2f} seconds"
                )

                img = np.copy(np.asarray(BufArr[bufnum], dtype=np.uint16))
                CirAq.AqCleanup()
                CirAq.BufferCleanup()
                CirAq.Close()

                # Create FITS header and save
                now = datetime.now(timezone.utc).replace(microsecond=0)
                curtime = now.strftime("%Y%m%dT%H%M%S")
                folder = self.parent.save_path_input.text().strip()
                os.makedirs(folder, exist_ok=True)

                # Create header
                hdr = fits.Header()
                mjd = now.timestamp() / 86400.0 + 40587
                hdr["MJD-OBS"] = (mjd, "Modified Julian Date of observation")

                object_name = self.parent.object_input.text().strip()
                observer_name = self.parent.observer_input.text().strip()
                if object_name:
                    hdr["OBJECT"] = (object_name, "Object name")
                if observer_name:
                    hdr["OBSERVER"] = (observer_name, "Observer name")

                # Query camera parameters
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

                self.parent.setup_serial()
                time.sleep(0.2)
                for key, (cmd, comment) in queries.items():
                    val = self.parent.query_scalar(cmd)
                    if val is not None and not val.startswith(cmd):
                        try:
                            if key in ["EXPTIME", "FRMTIME"] and val.isdigit():
                                val = int(val)
                                val = val / (CLOCK_FREQ_MHZ * 1e6)
                                val = np.round(val, 3)
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
                            self.parent.print_terminal(
                                f"Error converting {key} with value '{val}': {e}"
                            )

                # Add custom headers
                if self.custom_headers:
                    self.parent._add_custom_headers(hdr, self.custom_headers)

                # Add frame-specific info for stacks
                if self.save_as_stack:
                    hdr["FRAME"] = (i + 1, "Frame number in stack")
                    hdr["DATEOBS"] = (now.isoformat(), "Date-time of this frame")

                self.parent.CL.SerialClose()

                # Save based on mode
                if self.save_as_stack:
                    # Store for stack
                    image_stack.append(img)
                    stack_headers.append(hdr)
                    if i == 0:
                        if self.custom_filename:
                            base_filename = self.custom_filename
                            if not base_filename.endswith(".fits"):
                                base_filename += ".fits"
                            stack_filename = os.path.join(folder, base_filename)
                        else:
                            stack_filename = os.path.join(
                                folder, "scicam_stack_" + curtime + ".fits"
                            )
                    self.status_update.emit(
                        f"Collected frame {i+1}/{self.nframes} for stack"
                    )
                    # Send image for display
                    self.image_ready.emit(img)
                else:
                    # Save individual file
                    if self.custom_filename and self.nframes == 1:
                        base_filename = self.custom_filename
                        if not base_filename.endswith(".fits"):
                            base_filename += ".fits"
                        filename = os.path.join(folder, base_filename)
                    else:
                        filename = os.path.join(folder, "scicam_" + curtime + ".fits")

                    hdu = fits.PrimaryHDU(img, header=hdr)
                    hdu.writeto(filename, overwrite=True)
                    self.status_update.emit(f"Saved: {filename}")

                    # Send notification
                    notification = {
                        "event": "frame_saved",
                        "filename": filename,
                        "frame": i + 1,
                        "total_frames": self.nframes,
                    }
                    self.notification.emit(notification)

                    # Send image for display
                    self.image_ready.emit(img)

                if i == self.nframes - 1:
                    self.parent.setup_serial()
                    time.sleep(0.2)

            # If stacking, save the stack now
            if self.save_as_stack and image_stack:
                self.parent.print_terminal(
                    f"Saving stack of {len(image_stack)} frames..."
                )

                # Create 3D array
                data_cube = np.array(image_stack, dtype=np.uint16)

                # Create FITS with stack
                primary_hdr = stack_headers[0].copy()
                primary_hdr["NAXIS"] = 3
                primary_hdr["NAXIS3"] = len(image_stack)
                primary_hdr["NFRAMES"] = (len(image_stack), "Number of frames in stack")
                primary_hdr["STACKTYP"] = ("TEMPORAL", "Type of stack")

                primary_hdu = fits.PrimaryHDU(data_cube, header=primary_hdr)
                hdul = fits.HDUList([primary_hdu])

                # Add frame metadata
                for idx, hdr in enumerate(stack_headers):
                    col1 = fits.Column(name="FRAME", format="I", array=[idx + 1])
                    col2 = fits.Column(
                        name="MJD_OBS", format="D", array=[hdr["MJD-OBS"]]
                    )
                    cols = fits.ColDefs([col1, col2])
                    tbhdu = fits.BinTableHDU.from_columns(cols)
                    tbhdu.header["EXTNAME"] = f"FRAME{idx+1}"
                    for key in ["MJD-OBS", "TMP_CUR", "DATEOBS"]:
                        if key in hdr:
                            tbhdu.header[key] = hdr[key]
                    hdul.append(tbhdu)

                hdul.writeto(stack_filename, overwrite=True)
                hdul.close()

                self.status_update.emit(f"Saved stack: {stack_filename}")
                self.parent.print_terminal(f"Stack saved: {stack_filename}")

                # Send notification
                notification = {
                    "event": "stack_saved",
                    "filename": stack_filename,
                    "frames": len(image_stack),
                    "total_frames": self.nframes,
                }
                self.notification.emit(notification)

            self.capture_complete.emit()

        except Exception as e:
            self.capture_error.emit(str(e))
            self.parent.print_terminal(f"Capture error: {e}")


class CameraState(Enum):
    READY = auto()
    SETTING_EXPOSURE = auto()
    TEC_SETTLING = auto()
    EXPOSING = auto()
    ERROR = auto()


class SciCamGUI(QWidget):
    waiting_on_exposure_update = False

    def __init__(self, enable_server=True, server_port=5555):
        super().__init__()
        self.setWindowTitle("PIRT Control Panel")
        self.setGeometry(100, 100, 600, 550)  # Slightly taller for progress bar
        self.setup_ui()
        self.setup_serial()

        self.exp_input.blockSignals(True)
        self.exp_input.setValue(1.0)
        self.exp_input.blockSignals(False)

        # Query current exposure time from camera and update the display value
        self.exp_input.blockSignals(True)
        current_exp_str = self.query_scalar("SENS:EXPPER?")
        if current_exp_str:
            try:
                CLOCK_FREQ = 15.0
                current_cycles = int(current_exp_str)
                current_exp_seconds = current_cycles / (CLOCK_FREQ * 1e6)
                self.exp_input.setValue(current_exp_seconds)
                self.print_terminal(
                    f"Current camera exposure: {current_exp_seconds:.6f}s"
                )
            except ValueError:
                self.print_terminal(
                    f"Could not parse exposure cycles: {current_exp_str}, using default 1.0s"
                )
                self.exp_input.setValue(1.0)
        else:
            self.print_terminal(
                "Could not read exposure from camera, using default 1.0s"
            )
            self.exp_input.setValue(1.0)
        self.exp_input.blockSignals(False)

        self.state = {}

        # Enum to track the camera gui state
        self.camera_state = CameraState.READY

        # Add capture tracking variables
        self.is_capturing = False
        self.capture_start_time = None
        self.current_frame = 0
        self.total_frames = 0
        self.frame_start_time = None
        self.current_exposure_time = 0.0

        # Initialize capture thread
        self.capture_thread = CaptureThread(self)
        self.capture_thread.status_update.connect(self.on_capture_status_update)
        self.capture_thread.frame_captured.connect(self.on_frame_captured)
        self.capture_thread.capture_complete.connect(self.on_capture_complete)
        self.capture_thread.capture_error.connect(self.on_capture_error)
        self.capture_thread.image_ready.connect(self.on_image_ready)
        self.capture_thread.notification.connect(self.send_notification)

        self.update_status_indicators()

        # Slow timer for status queries (5 seconds)
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.update_status_indicators)
        self.status_timer.start(5000)

        # Fast timer for time updates (0.5 seconds)
        self.time_update_timer = QTimer()
        self.time_update_timer.timeout.connect(self.update_time_fields)
        self.time_update_timer.start(500)  # 2Hz update rate

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
                    self.print_terminal("Starting capture from remote command...")
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
                raw_path = cmd_data.get("path")

                if (
                    not raw_path
                    or not isinstance(raw_path, str)
                    or not raw_path.strip()
                ):
                    response = {"status": "error", "message": "No path provided"}
                else:
                    display_path = raw_path  # keep exactly what user entered for the UI
                    try:
                        # Expand ~ and any environment variables, then normalize
                        expanded = os.path.expandvars(os.path.expanduser(raw_path))
                        if not expanded.strip():
                            raise ValueError("Path resolves to an empty string")

                        p = Path(expanded)

                        # If something exists there and it's not a directory, fail early
                        if p.exists() and not p.is_dir():
                            raise NotADirectoryError(
                                f"Path exists and is not a directory: {p}"
                            )

                        # Create directory and any missing parents
                        p.mkdir(parents=True, exist_ok=True)

                        # Show the original (unexpanded) path in the UI
                        self.save_path_input.setText(display_path)

                        response = {
                            "status": "success",
                            "message": f"Save path set to {display_path}",
                            # Optionally include the resolved path for callers/logging
                            "resolved_path": str(p),
                        }
                    except Exception as e:
                        response = {
                            "status": "error",
                            "message": f"Failed to create directory '{display_path}': {e}",
                        }

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
                # print(f"Current state: {self.state}")
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
                    "tec_enabled": self.state.get("tec_enabled", default),
                    "tec_voltage": self.state.get("tec_voltage", default),
                    "case_temp": self.state.get("case_temp", default),
                    "digpcb_temp": self.state.get("digpcb_temp", default),
                    "senspcb_temp": self.state.get("senspcb_temp", default),
                    "waiting_on_exposure_update": int(self.waiting_on_exposure_update),
                    "ready": self.state.get("ready", False),
                    "timeout_remaining": self.state.get("timeout_remaining", 0.0),
                    "is_capturing": self.state.get("is_capturing", False),
                    "current_frame": self.state.get("current_frame", 0),
                    "total_frames": self.state.get("total_frames", 0),
                    "capture_time_remaining": self.state.get(
                        "capture_time_remaining", 0.0
                    ),
                    "camera_state": self.camera_state.name,
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
            if self.command_server:
                # Only print to terminal for non-GET_STATUS commands to reduce spam
                if cmd_type != "GET_STATUS":
                    self.print_terminal(
                        f"Sending response: {response['status']} - {response.get('message', '')}"
                    )
                self.command_server.send_response(json.dumps(response))

        except json.JSONDecodeError:
            response = {"status": "error", "message": "Invalid JSON command"}
            if self.command_server:
                self.command_server.send_response(json.dumps(response))
            import traceback

            self.print_terminal(f"Error processing command: {traceback.format_exc()}")
        except Exception as e:
            response = {"status": "error", "message": str(e)}
            if self.command_server:
                self.command_server.send_response(json.dumps(response))
            self.print_terminal(f"Error processing command: {e}")

    def update_time_fields(self):
        """Fast update for time-based fields only"""
        # Calculate ready status
        tec_locked = self.state.get("tec_lock", 0) == 1

        # Check what CameraState we are in
        if self.is_capturing:
            self.camera_state = CameraState.EXPOSING
        elif self.waiting_on_exposure_update:
            self.camera_state = CameraState.SETTING_EXPOSURE
        elif tec_locked:
            self.camera_state = CameraState.READY
        else:
            # TEC is not locked
            self.camera_state = CameraState.TEC_SETTLING

        self.state.update({"camera_state": self.camera_state.name})

        # Update the GUI:
        # Update camera state label
        self.camera_state_label.setText(f"State: {self.camera_state.name}")
        # Color code the state
        if self.camera_state == CameraState.READY:
            self.camera_state_label.setStyleSheet("font-weight: bold; color: green;")
        elif self.camera_state == CameraState.SETTING_EXPOSURE:
            self.camera_state_label.setStyleSheet("font-weight: bold; color: orange;")
        elif self.camera_state == CameraState.EXPOSING:
            self.camera_state_label.setStyleSheet("font-weight: bold; color: blue;")
        elif self.camera_state == CameraState.TEC_SETTLING:
            self.camera_state_label.setStyleSheet("font-weight: bold; color: purple;")
        elif self.camera_state == CameraState.ERROR:
            self.camera_state_label.setStyleSheet("font-weight: bold; color: red;")

        is_ready = (
            not self.waiting_on_exposure_update and tec_locked and not self.is_capturing
        )
        self.state.update({"ready": is_ready})

        # Calculate timeout remaining for exposure update
        timeout_remaining = 0.0
        if self.waiting_on_exposure_update and hasattr(self, "exposure_wait_end_time"):
            timeout_remaining = max(0.0, self.exposure_wait_end_time - time.time())
        self.state.update({"timeout_remaining": timeout_remaining})

        # Update capture/exposure status
        self.state.update({"is_capturing": self.is_capturing})
        self.state.update({"current_frame": self.current_frame})
        self.state.update({"total_frames": self.total_frames})

        # Calculate capture time remaining
        capture_time_remaining = 0.0
        exposure_time_remaining = 0.0

        if self.is_capturing:
            if self.frame_start_time:
                frame_elapsed = time.time() - self.frame_start_time
                expected_frame_time = self.current_exposure_time + 2.0
                frame_remaining = max(0.0, expected_frame_time - frame_elapsed)

                # Calculate exposure time remaining (without overhead)
                exposure_time_remaining = max(
                    0.0, self.current_exposure_time - frame_elapsed
                )
            else:
                frame_remaining = self.current_exposure_time + 2.0
                exposure_time_remaining = self.current_exposure_time

            frames_left = self.total_frames - self.current_frame
            remaining_frames_time = frames_left * (self.current_exposure_time + 2.0)
            capture_time_remaining = frame_remaining + remaining_frames_time

            # Update progress bar for exposure time
            if self.current_exposure_time > 0:
                # Calculate elapsed time
                exposure_elapsed = self.current_exposure_time - exposure_time_remaining

                # Set maximum to exposure time in tenths of seconds for smooth animation
                max_val = int(self.current_exposure_time * 10)
                current_val = int(exposure_elapsed * 10)
                self.capture_progress.setMaximum(max_val)
                self.capture_progress.setValue(current_val)
                self.capture_progress.setFormat(
                    f"Exposure: {exposure_time_remaining:.1f}s remaining"
                )
        else:
            # Reset progress bar when not capturing
            self.capture_progress.setValue(0)
            self.capture_progress.setFormat("Exposure: Idle")

        self.state.update({"capture_time_remaining": capture_time_remaining})

        # Update visual indicators
        if is_ready:
            self.ready_light.setStyleSheet(
                "background-color: green; border-radius: 8px;"
            )
        else:
            self.ready_light.setStyleSheet(
                "background-color: yellow; border-radius: 8px;"
            )

        # Update timeout label
        if timeout_remaining > 0:
            self.timeout_label.setText(f"Timeout: {timeout_remaining:.1f}s")
        else:
            self.timeout_label.setText("Timeout: N/A")

        # Update capture status labels
        if self.is_capturing:
            self.capture_status_label.setText(
                f"Capture: Frame {self.current_frame}/{self.total_frames}"
            )
            self.capture_progress_label.setText(
                f"Total time remaining: {capture_time_remaining:.1f}s"
            )
        else:
            self.capture_status_label.setText("Capture: Idle")
            self.capture_progress_label.setText("Progress: N/A")

    def on_capture_status_update(self, status):
        """Handle status updates from capture thread"""
        self.status_label.setText(status)

    def on_frame_captured(self, current, total):
        """Handle frame capture progress"""
        self.current_frame = current
        self.total_frames = total
        self.frame_start_time = time.time()  # Reset timer for new frame's exposure
        # Progress bar updates are now handled by update_time_fields()

    def on_capture_complete(self):
        """Handle capture completion"""
        self.is_capturing = False
        self.capture_start_time = None
        self.current_frame = 0
        self.total_frames = 0
        self.frame_start_time = None
        self.custom_headers = {}
        self.save_as_stack = False
        self.custom_filename = None
        self.status_label.setText("Status: Idle")
        self.capture_progress.setValue(0)

    def on_capture_error(self, error_msg):
        """Handle capture errors"""
        self.print_terminal(f"Capture error: {error_msg}")
        self.camera_state = CameraState.ERROR
        self.state.update({"camera_state": self.camera_state.name})
        self.on_capture_complete()  # Reset state
        # Keep the state in error until next successful operation that is not GET_STATUS
        # TODO:Add better handling of errors and thoughtful recovery

    def on_image_ready(self, img):
        """Handle new image for display"""
        if hasattr(self, "viewer") and self.viewer:
            self.viewer.update_image(img)
            QApplication.processEvents()

    def send_notification(self, notification):
        """Send notification to TCP clients"""
        if self.command_server:
            self.command_server.send_response(json.dumps(notification))

    def update_status_indicators(self):

        # Skip all serial queries if we're currently capturing
        if self.is_capturing:
            self.print_terminal("Skipping status queries during exposure")
            return
        else:
            # Query the camera status over the serial connection
            setpoint = self.query_scalar("TEMP:SENS:SET?")

            if setpoint:
                try:
                    setpoint = np.round(float(setpoint), 2)
                except Exception:
                    setpoint = -888.0
                self.tec_setpoint_label.setText(f"Setpoint (°C): {setpoint}")

            self.state.update({"tec_setpoint": setpoint})

            temp = self.query_scalar("TEMP:SENS?")
            if temp:
                try:
                    temp = np.round(float(temp), 2)
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
            tec_lock_status = 0  # Default to not locked

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
                    case_temp = np.round(float(case_temp), 2)
                except Exception:
                    case_temp = -888.0
            self.state.update({"case_temp": case_temp})

            # DIGPCB Temperature
            digpcb_temp = self.query_scalar("TEMP:DIGPCB?")
            if digpcb_temp:
                try:
                    digpcb_temp = np.round(float(digpcb_temp), 2)
                except Exception:
                    digpcb_temp = -888.0
            self.state.update({"digpcb_temp": digpcb_temp})

            # SENSPCB Temperature
            senspcb_temp = self.query_scalar("TEMP:SENSPCB?")
            if senspcb_temp:
                try:
                    senspcb_temp = np.round(float(senspcb_temp), 2)
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

        # Add ready status indicator
        self.ready_light = QLabel()
        self.ready_light.setFixedSize(16, 16)
        self.ready_light.setStyleSheet("background-color: gray; border-radius: 8px;")
        ready_row = QHBoxLayout()
        ready_row.addWidget(QLabel("Ready:"))
        ready_row.addWidget(self.ready_light)

        # Add camera state label (add this after the ready indicator)
        self.camera_state_label = QLabel("State: READY")
        self.camera_state_label.setStyleSheet("font-weight: bold;")
        labels_layout.addWidget(self.camera_state_label)

        labels_layout.addWidget(self.soc_label)
        labels_layout.addWidget(QLabel("Gain Corr:"))
        labels_layout.addWidget(self.gaincor_dropdown)
        labels_layout.addWidget(QLabel("Offset Corr:"))
        labels_layout.addWidget(self.offcor_dropdown)
        labels_layout.addWidget(QLabel("Subst Corr:"))
        labels_layout.addWidget(self.subcor_dropdown)
        labels_layout.addLayout(tec_lock_row)
        labels_layout.addLayout(ready_row)

        # Add timeout remaining label
        self.timeout_label = QLabel("Timeout: N/A")
        labels_layout.addWidget(self.timeout_label)

        # Add capture status indicators
        self.capture_status_label = QLabel("Capture: Idle")
        labels_layout.addWidget(self.capture_status_label)

        self.capture_progress_label = QLabel("Progress: N/A")
        labels_layout.addWidget(self.capture_progress_label)

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
        default_folder = os.path.expanduser("~/data")
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

        # Add progress bar below capture button - configured for exposure time
        self.capture_progress = QProgressBar()
        self.capture_progress.setTextVisible(True)
        self.capture_progress.setFormat("Exposure: Idle")
        self.capture_progress.setStyleSheet(
            """
            QProgressBar {
                text-align: center;
                border: 1px solid grey;
                border-radius: 3px;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
                border-radius: 2px;
            }
        """
        )
        layout.addWidget(self.capture_progress)

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
        new_exp = self.exp_input.value()

        # Check if exposure is actually changing
        current_exp_str = self.query_scalar("SENS:EXPPER?")
        if current_exp_str:
            try:
                CLOCK_FREQ = 15.0
                current_cycles = int(current_exp_str)
                current_exp_seconds = np.round(current_cycles / (CLOCK_FREQ * 1e6), 3)

                # If exposure is within 0.001s of current, no need to change
                if abs(current_exp_seconds - new_exp) < 0.001:
                    self.print_terminal(
                        f"Exposure already set to {new_exp}s, no change needed"
                    )
                    return True
            except ValueError:
                pass  # Continue with normal exposure setting if can't parse

        # Proceed with exposure change
        self.waiting_on_exposure_update = True
        self.exposure_update_start_time = time.time()

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
        except Exception as e:
            self.print_terminal(f"Error during TEC temperature change: {e}")

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
        """Start capture in separate thread"""
        # Wait for exposure update if needed
        if self.waiting_on_exposure_update:
            self.print_terminal("Cannot capture while exposure is updating")
            return

        try:
            nframes = int(self.nframes_input.text().strip())
        except ValueError:
            self.print_terminal("Invalid number of frames; defaulting to 1")
            nframes = 1

        # Setup and start capture thread
        self.capture_thread.setup_capture(
            nframes, self.save_as_stack, self.custom_headers, self.custom_filename
        )

        if not self.capture_thread.isRunning():
            self.capture_thread.start()
        else:
            self.print_terminal("Capture already in progress!")

    def _add_custom_headers(self, hdr, headers_input):
        """Add custom headers from various input formats"""
        try:
            # If it's a dictionary
            if isinstance(headers_input, dict):
                for key, value in headers_input.items():
                    if isinstance(value, (list, tuple)) and len(value) >= 2:
                        # (key, value, comment) format
                        header_value = value[0]
                        comment = value[1] if len(value) > 1 else ""

                        # Replace None with empty string for FITS compatibility
                        if header_value is None:
                            header_value = ""
                            self.print_terminal(
                                f"Replacing None value in header {key} with empty string"
                            )

                        hdr[key.upper()] = (header_value, comment)
                    else:
                        # Replace None with empty string
                        if value is None:
                            value = ""
                            self.print_terminal(
                                f"Replacing None value in header {key} with empty string"
                            )

                        hdr[key.upper()] = value

            # If it's a list of tuples or Card objects
            elif isinstance(headers_input, list):
                for item in headers_input:
                    if isinstance(item, (list, tuple)):
                        if len(item) >= 2:
                            key = str(item[0]).upper()
                            value = item[1]
                            comment = item[2] if len(item) > 2 else ""

                            # Replace None with empty string for FITS compatibility
                            if value is None:
                                value = ""
                                self.print_terminal(
                                    f"Replacing None value in header {key} with empty string"
                                )

                            hdr[key] = (value, comment)
                    elif hasattr(item, "keyword") and hasattr(item, "value"):
                        # Astropy Card object
                        value = item.value
                        if value is None:
                            value = ""
                            self.print_terminal(
                                f"Replacing None value in header {item.keyword} with empty string"
                            )

                        hdr[item.keyword] = (
                            value,
                            item.comment if hasattr(item, "comment") else "",
                        )

        except Exception as e:
            self.print_terminal(f"Error adding custom headers: {e}")

    def closeEvent(self, event):
        """Clean up when window is closed"""
        if self.command_server:
            self.command_server.stop()
            self.command_server.wait()
        # Stop capture thread if running
        if self.capture_thread.isRunning():
            self.capture_thread.terminate()
            self.capture_thread.wait()
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
