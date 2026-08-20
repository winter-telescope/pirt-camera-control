import os
import sys

# Add required DLL directories for BitFlow SDK and Camera Link Serial driver
os.add_dll_directory(r"C:\BitFlow SDK 6.5\Bin64")
os.add_dll_directory(r"C:\Program Files\CameraLink\Serial")

import getopt
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
import pyqtgraph as pg
from astropy.io import fits
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
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


class TrigMode(str, Enum):
    SINGLE = "SINGLE"
    STREAM = "STREAM"


class FrameTimeMode(str, Enum):
    """How the frame period is determined when the exposure time changes.

    AUTO:  frame time tracks exposure time, frame = exposure + overhead. This is
           the historical behavior and the right default for normal observing.
    FIXED: frame time is pinned at a user-supplied value and does not move when
           the exposure changes. Needed for PTC ramps, where the frame rate must
           stay constant while the integration time is swept.
    """

    AUTO = "AUTO"
    FIXED = "FIXED"


# Camera pixel clock. The camera works in clock cycles; everything the user sees
# is in seconds.
CLOCK_FREQ_MHZ = 15.0
CLOCK_FREQ_HZ = CLOCK_FREQ_MHZ * 1e6

# Firmware limits on SENS:EXPPER / SENS:FRAMEPER, in clock cycles.
MIN_EXPOSURE_CYCLES = 12
MAX_EXPOSURE_CYCLES = 4294967142
MIN_FRAME_CYCLES = 1800
MAX_FRAME_CYCLES = 4294967295

# Padding between exposure and frame period. The frame period must exceed the
# exposure period for readout; MIN_FRAME_OVERHEAD_S is our assumed floor for
# that margin and DEFAULT_FRAME_OVERHEAD_S is what AUTO mode adds.
DEFAULT_FRAME_OVERHEAD_S = 0.1
MIN_FRAME_OVERHEAD_S = 0.1

# Per-frame software overhead on top of the camera's frame period: serial
# setup, arming the grabber, and writing the FITS file. Used only for progress
# and time-remaining estimates, never for camera configuration.
CAPTURE_OVERHEAD_S = 2.0

# Slack added to the frame period when waiting for a single triggered frame.
SINGLE_TRIGGER_OVERHEAD_S = 1.0


def sec_to_cycles(seconds: float) -> int:
    """Convert seconds to camera clock cycles."""
    return int(seconds * CLOCK_FREQ_HZ)


def cycles_to_sec(cycles: float) -> float:
    """Convert camera clock cycles to seconds."""
    return cycles / CLOCK_FREQ_HZ


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
                            # Only print received command if it's not GET_STATUS
                            try:
                                cmd_data = json.loads(command)
                                if cmd_data.get("command", "").upper() != "GET_STATUS":
                                    print(
                                        f"Received command: {command[:100]}..."
                                    )  # Print first 100 chars
                            except:
                                # If we can't parse it, print it anyway
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

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PIRT Quick Look Viewer")
        self.setGeometry(750, 100, 600, 800)

        # --- UI ---

        self._pg = pg  # stash to avoid re-imports

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # Graphics layout holds both the image and the histogram plot
        self.glw = pg.GraphicsLayoutWidget()
        root.addWidget(self.glw)

        # Top: image view with square pixels (locked aspect)
        self.img_vb = self.glw.addViewBox(
            row=0, col=0, lockAspect=True, enableMenu=False
        )
        self.img_vb.setDefaultPadding(0.0)
        self.img_item = pg.ImageItem(axisOrder="row-major")
        self.img_vb.addItem(self.img_item)

        # Bottom: histogram plot (vector graphics, scales cleanly)
        self.glw.nextRow()
        self.hist_plot: pg.PlotItem = self.glw.addPlot(row=1, col=0)
        self.hist_plot.showGrid(x=True, y=True, alpha=0.2)
        self.hist_plot.setLabel("bottom", "ADU")
        self.hist_plot.setLabel("left", "Count")
        self.hist_curve = self.hist_plot.plot(
            stepMode=True, fillLevel=0, brush=(200, 200, 200, 160)
        )
        self.hist_plot.setMenuEnabled(False)
        self.hist_plot.setMouseEnabled(x=True, y=False)

        # a grayscale LUT (optional; looks nice)
        lut = self._pg.ColorMap(
            [0.0, 1.0], [[0, 0, 0], [255, 255, 255]]
        ).getLookupTable(0.0, 1.0, 256)
        self.img_item.setLookupTable(lut)
        self.img_item.setLevels([0, 255])

        # cached last image shape (to set correct view range once)
        self._last_shape = None

        # initial font sizing
        self._apply_axis_fonts()

    def _apply_axis_fonts(self):
        """Scale axis/tick fonts based on the widget height so labels stay readable."""
        import pyqtgraph as pg

        h = max(1, self.height())
        # heuristic: 2.5% of height for titles, 2% for ticks; clamp to [7, 14]
        tick_pt = int(max(7, min(14, 0.020 * h)))
        title_pt = int(max(8, min(16, 0.025 * h)))

        ax = self.hist_plot.getAxis("bottom")
        ay = self.hist_plot.getAxis("left")
        ax.setStyle(tickFont=self._pg.QtGui.QFont("", tick_pt))
        ay.setStyle(tickFont=self._pg.QtGui.QFont("", tick_pt))
        self.hist_plot.getAxis("bottom").setHeight(max(18, tick_pt * 2))
        self.hist_plot.getAxis("left").setWidth(max(28, tick_pt * 2))

        # Title as part of plot (optional)
        # self.hist_plot.setTitle("Histogram", size=f"{title_pt}pt")

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply_axis_fonts()

    def update_image(self, data: np.ndarray):
        """Render clipped image + histogram, with square-pixel image and vector histogram."""
        import numpy as np

        # --- robust clip (zscale-like simple 3σ around median) ---
        median = float(np.median(data))
        std = float(np.std(data))
        lower = median - 3 * std
        upper = median + 3 * std
        clipped = np.clip(data, lower, upper)
        # normalize to 0..255 for display; keep underlying dtype for FITS saving elsewhere
        denom = (upper - lower) if upper > lower else 1.0
        norm = ((clipped - lower) * (255.0 / denom)).astype(np.uint8)

        # --- image display ---
        # pyqtgraph accepts 2D arrays directly; square pixels guaranteed by lockAspect=True
        self.img_item.setImage(norm, autoLevels=False)

        # set view range once (keeps square aspect without stretching)
        if self._last_shape != norm.shape:
            h, w = norm.shape
            self.img_vb.setRange(xRange=(0, w), yRange=(0, h), padding=0.0)
            self._last_shape = norm.shape

        # --- histogram (vector, no raster scaling) ---
        # bins focused around the same clipping range for consistency
        nbins = 256
        hist_range = (lower, upper) if std > 0 else (median - 1, median + 1)
        counts, edges = np.histogram(data.ravel(), bins=nbins, range=hist_range)
        # step plot: use edges as x, prepend a zero for visual pad if desired
        self.hist_curve.setData(x=edges, y=counts)

        # axis range: leave a little headroom
        self.hist_plot.setXRange(*hist_range, padding=0.02)
        ymax = max(1, counts.max())
        self.hist_plot.setYRange(0, ymax * 1.1)

        # Optional subtitle text (no squish)
        mean = float(np.mean(data))
        mode = int(np.bincount(data.ravel()).argmax()) if data.size > 0 else 0
        self.hist_plot.setTitle(
            f"Mean: {mean:.1f} | Med: {median:.1f} | Mode: {mode} | Std: {std:.1f}",
            size="10pt",
        )


class CaptureThread(QThread):
    """Thread for capturing frames without blocking the GUI"""

    status_update = pyqtSignal(str)
    frame_captured = pyqtSignal(int, int)  # current, total
    capture_complete = pyqtSignal()
    capture_error = pyqtSignal(str)
    image_ready = pyqtSignal(np.ndarray)
    notification = pyqtSignal(dict)

    def __init__(self, parent=None, trigmode: TrigMode = TrigMode.SINGLE):
        super().__init__(parent)
        self.parent = parent
        self.nframes = 1
        self.save_as_stack = False
        self.custom_headers = {}
        self.trigmode = trigmode
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

                # SINGLE trigger mode logic:
                if self.trigmode is TrigMode.SINGLE:
                    CirAq = None
                    try:
                        # 1) Setup camera for single-shot INT mode
                        self.parent.setup_serial()
                        self.parent.send_command("SENS:TRIG:MODE INT")
                        time.sleep(0.05)
                        self.parent.send_command("SENS:TRIG OFF")
                        time.sleep(0.10)
                        self.parent.send_command("SENS:TRIG:COUNT 1")
                        time.sleep(0.05)

                        # Query baseline state
                        cnt0 = self.parent.query_scalar("SENS:TRIG:COUNT?")
                        sent0 = self.parent.query_scalar("SENS:TRIG:SENT?")
                        self.parent.print_terminal(
                            f"[single] COUNT baseline: {cnt0}, SENT baseline: {sent0}"
                        )

                        # 2) Close serial and arm the frame grabber FIRST
                        self.parent.CL.SerialClose()

                        CirAq = Buf.clsCircularAcquisition(Buf.ErrorMode.ErIgnore)
                        CirAq.Open(0)
                        numbuffers = 3
                        BufArr = CirAq.BufferSetup(numbuffers)
                        CirAq.AqSetup(Buf.SetupOptions.setupDefault)
                        CirAq.AqControl(
                            Buf.AcqCommands.Start, Buf.AcqControlOptions.Wait
                        )
                        time.sleep(0.05)

                        # 3) Fire the trigger
                        self.parent.setup_serial()
                        self.parent.send_command("SENS:TRIG ON")
                        t_start = time.time()
                        self.parent.CL.SerialClose()

                        # Get expected timing. The frame arrives one frame period
                        # after the trigger, not one exposure — these differ
                        # whenever the frame time is set independently.
                        exp_s = float(self.parent.current_exposure_time or 0.0)
                        frame_s = float(self.parent.current_frame_time or 0.0)
                        expected_time = frame_s + SINGLE_TRIGGER_OVERHEAD_S

                        self.parent.print_terminal(
                            f"[single] Trigger fired. Exposure: {exp_s:.1f}s, "
                            f"frame: {frame_s:.1f}s, expected: ~{expected_time:.1f}s"
                        )

                        # 4) Wait for frame with BitFlow timeout workaround
                        # BitFlow WaitForFrame times out ~9-10s, so we must reinit grabber periodically
                        # This is a BitFlow driver quirk, not a camera issue
                        framearr = False
                        attempt = 0
                        max_attempts = 10  # Allow more attempts for very long exposures

                        # Use 8s timeout chunks (under the ~9-10s BitFlow limit)
                        wait_chunk_ms = 8000
                        absolute_deadline = (
                            t_start + expected_time + 15.0
                        )  # Expected + 15s buffer

                        while (
                            not framearr
                            and time.time() < absolute_deadline
                            and attempt < max_attempts
                        ):
                            attempt += 1
                            elapsed = time.time() - t_start
                            remaining = absolute_deadline - time.time()

                            # Calculate timeout for this attempt
                            wait_ms = min(wait_chunk_ms, int(remaining * 1000))
                            if wait_ms < 100:
                                break

                            try:
                                curBuf = CirAq.WaitForFrame(wait_ms)
                                framearr = True
                                bufnum = curBuf.BufferNumber

                            except Buf.PythonMemException:
                                # BitFlow timeout hit - this is expected for long exposures
                                elapsed = time.time() - t_start

                                # Check camera status periodically (every other timeout)
                                if attempt % 2 == 0:
                                    try:
                                        self.parent.setup_serial()
                                        cur_sent = self.parent.query_scalar(
                                            "SENS:TRIG:SENT?"
                                        )
                                        self.parent.CL.SerialClose()

                                        if (
                                            cur_sent
                                            and sent0
                                            and int(cur_sent) > int(sent0)
                                        ):
                                            self.parent.print_terminal(
                                                f"[single] t={elapsed:.1f}s: Camera finished (SENT={cur_sent}), "
                                                "waiting for DMA..."
                                            )
                                        elif elapsed < expected_time * 0.9:
                                            # Don't spam during normal exposure
                                            pass
                                        else:
                                            self.parent.print_terminal(
                                                f"[single] t={elapsed:.1f}s: Still waiting (SENT={cur_sent})..."
                                            )
                                    except Exception as e:
                                        self.parent.print_terminal(
                                            f"[single] Status check failed: {e}"
                                        )

                                # Reinitialize grabber (required for BitFlow with long waits)
                                try:
                                    CirAq.AqCleanup()
                                    CirAq.BufferCleanup()
                                    CirAq.Close()
                                except:
                                    pass

                                if (
                                    time.time() < absolute_deadline
                                    and attempt < max_attempts
                                ):
                                    CirAq = Buf.clsCircularAcquisition(
                                        Buf.ErrorMode.ErIgnore
                                    )
                                    CirAq.Open(0)
                                    BufArr = CirAq.BufferSetup(numbuffers)
                                    CirAq.AqSetup(Buf.SetupOptions.setupDefault)
                                    CirAq.AqControl(
                                        Buf.AcqCommands.Start,
                                        Buf.AcqControlOptions.Wait,
                                    )
                                    time.sleep(0.05)

                        # Check if we got the frame
                        if not framearr or curBuf is None:
                            elapsed = time.time() - t_start

                            # Get final camera state
                            try:
                                self.parent.setup_serial()
                                sent_final = self.parent.query_scalar("SENS:TRIG:SENT?")
                                trig_final = self.parent.query_scalar("SENS:TRIG?")
                                self.parent.CL.SerialClose()

                                if (
                                    sent_final
                                    and sent0
                                    and int(sent_final) > int(sent0)
                                ):
                                    error_msg = (
                                        f"Camera exposed (SENT: {sent0}→{sent_final}) but frame never arrived "
                                        f"after {elapsed:.1f}s. Check Camera Link cable/connection."
                                    )
                                else:
                                    error_msg = (
                                        f"No frame after {elapsed:.1f}s (expected ~{expected_time:.1f}s). "
                                        f"Camera state: SENT={sent_final}, TRIG={trig_final}"
                                    )
                                raise TimeoutError(error_msg)

                            except TimeoutError:
                                raise
                            except Exception as e:
                                raise TimeoutError(
                                    f"No frame after {elapsed:.1f}s: {e}"
                                )

                        # 5) Success!
                        total_time = time.time() - t_start
                        self.parent.last_exposure_duration = total_time

                        self.parent.print_terminal(
                            f"[single] Frame received! Actual: {total_time:.2f}s "
                            f"(expected ~{expected_time:.1f}s)"
                        )

                        img = np.copy(np.asarray(BufArr[bufnum], dtype=np.uint16))

                        # 6) Verify SENT
                        try:
                            self.parent.setup_serial()
                            sent_final = self.parent.query_scalar("SENS:TRIG:SENT?")
                            self.parent.CL.SerialClose()

                            self.parent.print_terminal(
                                f"[single] SENT: {sent0} → {sent_final}"
                            )

                            if sent0 is not None and sent_final is not None:
                                if int(sent_final) <= int(sent0):
                                    self.parent.print_terminal(
                                        "[single] WARNING: SENT did not increment! Frame may be invalid."
                                    )

                        except Exception as e:
                            self.parent.print_terminal(
                                f"[single] SENT check failed: {e}"
                            )

                    finally:
                        # 7) Cleanup
                        try:
                            self.parent.setup_serial()
                            self.parent.send_command("SENS:TRIG OFF")
                            self.parent.CL.SerialClose()
                            self.parent.print_terminal("[single] Trigger disabled")
                        except Exception as e:
                            self.parent.print_terminal(
                                f"[single] Cleanup TRIG OFF failed: {e}"
                            )

                        if CirAq is not None:
                            try:
                                CirAq.AqCleanup()
                                CirAq.BufferCleanup()
                                CirAq.Close()
                            except Exception as e:
                                self.parent.print_terminal(
                                    f"[single] Cleanup grabber failed: {e}"
                                )

                # STREAM trigger mode logic:
                else:
                    CirAq = Buf.clsCircularAcquisition(Buf.ErrorMode.ErIgnore)
                    CirAq.Open(0)
                    numbuffers = 3
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
                            curBuf = CirAq.WaitForFrame(5000)
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

                # Store the actual exposure duration
                self.parent.last_exposure_duration = total_time

                self.parent.print_terminal(
                    f"[single] Frame received! Total time: {total_time:.2f}s"
                )

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

                hdr["TRIGMODE"] = (self.trigmode.value, "Acquisition trigger mode")
                hdr["FRMTMODE"] = (
                    self.parent.frame_time_mode.value,
                    "Frame time mode (AUTO/FIXED)",
                )

                # Add last exposure duration if available
                if self.parent.last_exposure_duration is not None:
                    hdr["AEXPTIME"] = (
                        round(self.parent.last_exposure_duration, 3),
                        "Actual exposure duration (s)",
                    )

                # Query camera parameters
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
                                val = np.round(cycles_to_sec(int(val)), 3)
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

    def __init__(
        self, enable_server=True, server_port=5555, trigmode: TrigMode = TrigMode.SINGLE
    ):
        super().__init__()
        self.trigmode = trigmode
        self.setWindowTitle("PIRT Control Panel")
        self.setGeometry(100, 100, 600, 600)  # Slightly taller for progress bar

        # query_scalar() touches these on every call, including the startup
        # queries below, so they have to exist before the first query.
        self.serial_error_count = 0  # Track consecutive serial errors
        self.max_serial_errors = 3  # Max errors before attempting reconnect

        # Frame timing state. Must exist before setup_ui() builds the widgets
        # that display it.
        self.frame_time_mode = FrameTimeMode.AUTO
        self.frame_time_overhead = DEFAULT_FRAME_OVERHEAD_S
        self.current_exposure_time = 0.0
        self.current_frame_time = 1.0 + DEFAULT_FRAME_OVERHEAD_S
        self.fixed_frame_time = self.current_frame_time

        self.setup_ui()
        self.setup_serial()

        self.exp_input.blockSignals(True)
        self.exp_input.setValue(1.0)
        self.exp_input.blockSignals(False)

        # Query current exposure and frame period from the camera and adopt them
        # as the displayed values.
        self.exp_input.blockSignals(True)
        current_cycles = self._query_cycles("SENS:EXPPER?")
        if current_cycles is not None:
            current_exp_seconds = cycles_to_sec(current_cycles)
            self.exp_input.setValue(current_exp_seconds)
            self.current_exposure_time = current_exp_seconds
            self.print_terminal(f"Current camera exposure: {current_exp_seconds:.6f}s")
        else:
            self.print_terminal(
                "Could not read exposure from camera, using default 1.0s"
            )
            self.exp_input.setValue(1.0)
            self.current_exposure_time = 1.0
        self.exp_input.blockSignals(False)

        current_frame_cycles = self._query_cycles("SENS:FRAMEPER?")
        if current_frame_cycles is not None:
            self.current_frame_time = cycles_to_sec(current_frame_cycles)
            self.print_terminal(
                f"Current camera frame time: {self.current_frame_time:.6f}s"
            )
        else:
            self.print_terminal(
                "Could not read frame period from camera, assuming "
                f"exposure + {self.frame_time_overhead}s"
            )
            self.current_frame_time = (
                self.current_exposure_time + self.frame_time_overhead
            )
        self.fixed_frame_time = self.current_frame_time
        self._sync_timing_display()

        self.state = {}

        # Enum to track the camera gui state
        self.camera_state = CameraState.READY

        # Add capture tracking variables
        self.is_capturing = False
        self.capture_start_time = None
        self.current_frame = 0
        self.total_frames = 0
        self.frame_start_time = None
        # current_exposure_time / current_frame_time are seeded from the camera
        # above; don't clobber them here.
        self.last_exposure_duration = None  # Track actual exposure time

        # Initialize capture thread
        self.capture_thread = CaptureThread(parent=self, trigmode=self.trigmode)
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
                nframes = cmd_data.get("nframes", 1)
                self.nframes_input.setText(str(nframes))

                self.custom_headers = cmd_data.get("headers", {})
                self.save_as_stack = cmd_data.get("stack", False)
                self.custom_filename = cmd_data.get("filename", None)

                if not self.capture_button.isEnabled():
                    response = {"status": "error", "message": "TEC not locked"}
                    if self.command_server:
                        self.command_server.send_response(json.dumps(response) + "\n")
                    return

                # Immediate ACK before starting the long/async work
                ack = {
                    "status": "success",
                    "message": f"Starting capture of {nframes} frame(s)",
                }
                if self.command_server:
                    # Log and send with a newline so line-based clients can read a frame
                    self.print_terminal(
                        f"Sending response: {ack['status']} - {ack.get('message','')}"
                    )
                    try:
                        self.command_server.send_response(json.dumps(ack) + "\n")
                    except Exception as e:
                        self.print_terminal(f"Failed to send ACK: {e}")
                        return  # don't proceed if we can't talk to the client

                self.print_terminal("Starting capture from remote command...")

                # Defer the actual capture so the socket write can flush
                QTimer.singleShot(0, self.capture_frame)

                return  # don't fall through to the common send-response block
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

            elif cmd_type == "SET_FRAME_TIME":
                # Set the frame period explicitly, holding the exposure fixed.
                frame_time = float(cmd_data.get("frame_time", 0.0))
                wait_for_completion = cmd_data.get("wait", True)

                self.frame_time_input.setValue(frame_time)
                if not self.on_frame_time_changed():
                    response = {
                        "status": "error",
                        "message": (
                            f"Failed to set frame time to {frame_time}s - "
                            f"out of range or shorter than the current exposure"
                        ),
                    }
                elif wait_for_completion and self.waiting_on_exposure_update:
                    wait_success = self.wait_for_exposure_update()
                    response = {
                        "status": "success" if wait_success else "warning",
                        "message": (
                            f"Frame time set to {frame_time}s and settled"
                            if wait_success
                            else f"Frame time set to {frame_time}s but wait timeout exceeded"
                        ),
                    }
                    if self.command_server:
                        self.print_terminal(
                            f"Sending response: {response['status']} - {response.get('message', '')}"
                        )
                        self.command_server.send_response(json.dumps(response))
                    return
                else:
                    response = {
                        "status": "success",
                        "message": f"Frame time set to {frame_time}s (not waiting for settle)",
                    }

            elif cmd_type == "SET_FRAME_TIME_MODE":
                # Switch frame timing between AUTO and FIXED.
                mode = cmd_data.get("mode", "")
                frame_time = cmd_data.get("frame_time", None)
                if frame_time is not None:
                    frame_time = float(frame_time)

                if self.set_frame_time_mode(mode, frame_time=frame_time):
                    self._begin_timing_wait(f"Frame time mode set to {mode}")
                    response = {
                        "status": "success",
                        "message": (
                            f"Frame time mode set to {self.frame_time_mode.value} "
                            f"(frame time {self.current_frame_time:.6f}s)"
                        ),
                    }
                else:
                    response = {
                        "status": "error",
                        "message": f"Failed to set frame time mode to {mode}",
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
                    "frame_time": self.current_frame_time,
                    "frame_time_mode": self.frame_time_mode.value,
                    "frame_time_overhead": self.frame_time_overhead,
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
                    "trigmode": self.trigmode.value,
                    "last_exposure_duration": self.state.get(
                        "last_exposure_duration", None
                    ),
                    "serial_error_count": self.serial_error_count,
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
            # Per-frame wall time is the camera's frame period plus our own
            # per-frame overhead (serial setup, grabber arm, FITS write).
            expected_frame_time = self.current_frame_time + CAPTURE_OVERHEAD_S

            if self.frame_start_time:
                frame_elapsed = time.time() - self.frame_start_time
                frame_remaining = max(0.0, expected_frame_time - frame_elapsed)

                # Calculate exposure time remaining (without overhead)
                exposure_time_remaining = max(
                    0.0, self.current_exposure_time - frame_elapsed
                )
            else:
                frame_remaining = expected_frame_time
                exposure_time_remaining = self.current_exposure_time

            frames_left = self.total_frames - self.current_frame
            remaining_frames_time = frames_left * expected_frame_time
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

        # Update last exposure duration display
        if self.last_exposure_duration is not None:
            self.last_exposure_label.setText(
                f"Last Exposure: {self.last_exposure_duration:.2f}s"
            )
            self.state.update({"last_exposure_duration": self.last_exposure_duration})
        else:
            self.last_exposure_label.setText("Last Exposure: N/A")
            self.state.update({"last_exposure_duration": None})

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

        # Add last exposure duration label
        self.last_exposure_label = QLabel("Last Exposure: N/A")
        labels_layout.addWidget(self.last_exposure_label)

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
        self.tec_temp_dropdown.addItems(["-20", "-40", "-50", "-55","-60"])
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

        # Frame time controls. In AUTO the spinbox is a read-only mirror of
        # exposure + overhead; in FIXED it becomes editable and the frame period
        # stops following the exposure.
        self.frame_time_label = QLabel("Frame Time (s):")
        layout.addWidget(self.frame_time_label)

        self.frame_time_input = QDoubleSpinBox()
        self.frame_time_input.setDecimals(6)
        self.frame_time_input.setRange(
            MIN_FRAME_OVERHEAD_S, 999.999999 + MIN_FRAME_OVERHEAD_S
        )
        self.frame_time_input.setSingleStep(0.01)
        self.frame_time_input.setValue(self.current_frame_time)
        self.frame_time_input.setEnabled(self.frame_time_mode is FrameTimeMode.FIXED)

        self.frame_time_mode_dropdown = QComboBox()
        self.frame_time_mode_dropdown.addItems(
            [FrameTimeMode.AUTO.value, FrameTimeMode.FIXED.value]
        )
        self.frame_time_mode_dropdown.setCurrentText(self.frame_time_mode.value)

        frame_time_row = QHBoxLayout()
        frame_time_row.addWidget(self.frame_time_input)
        frame_time_row.addWidget(QLabel("Mode:"))
        frame_time_row.addWidget(self.frame_time_mode_dropdown)
        layout.addLayout(frame_time_row)

        QTimer.singleShot(
            0, lambda: self.exp_input.editingFinished.connect(self.on_exposure_changed)
        )
        QTimer.singleShot(
            0,
            lambda: self.frame_time_input.editingFinished.connect(
                self.on_frame_time_changed
            ),
        )
        QTimer.singleShot(
            0,
            lambda: self.frame_time_mode_dropdown.currentTextChanged.connect(
                self.on_frame_time_mode_changed
            ),
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

    def _query_cycles(self, command):
        """Query a cycle-count register. Returns an int, or None if unreadable."""
        raw = self.query_scalar(command)
        try:
            return int(raw) if raw else None
        except ValueError:
            self.print_terminal(f"Warning: could not parse {command} response: {raw}")
            return None

    def _send_timing(self, command, cycles, label):
        """Send one timing register write. Returns True on success."""
        result = self.send_command(f"{command} {cycles}")
        if "Out of Range" in result or "ERROR" in result:
            self.print_terminal(f"ERROR: {label} out of range. Cycles: {cycles}")
            return False
        return True

    def apply_timing(self, exposure_s, frame_s):
        """Set exposure period and frame period on the camera.

        This is the single low-level entry point for camera timing. Both values
        are explicit — nothing here derives one from the other. Returns True on
        success; on failure the previous timing is restored where possible.
        """
        if frame_s < exposure_s + MIN_FRAME_OVERHEAD_S:
            self.print_terminal(
                f"ERROR: frame time {frame_s:.6f}s too short for exposure "
                f"{exposure_s:.6f}s (needs exposure + {MIN_FRAME_OVERHEAD_S}s)"
            )
            return False

        prev_exposure_cycles = self._query_cycles("SENS:EXPPER?")
        prev_frame_cycles = self._query_cycles("SENS:FRAMEPER?")

        exposure_cycles = max(
            MIN_EXPOSURE_CYCLES, min(sec_to_cycles(exposure_s), MAX_EXPOSURE_CYCLES)
        )
        frame_cycles = max(
            MIN_FRAME_CYCLES, min(sec_to_cycles(frame_s), MAX_FRAME_CYCLES)
        )

        self.print_terminal(
            f"Timing: exposure {prev_exposure_cycles} → {exposure_cycles} cycles, "
            f"frame {prev_frame_cycles} → {frame_cycles} cycles"
        )

        # Order matters: the camera rejects any state where the frame period is
        # shorter than the exposure period, so we always widen the window before
        # narrowing it. Growing the frame period first leaves the intermediate
        # state (new_frame, prev_exposure), which is safe because new_frame >
        # prev_frame >= prev_exposure. Shrinking it last leaves the intermediate
        # state (prev_frame, new_exposure), safe because prev_frame >= new_frame
        # >= new_exposure + overhead. Either way no intermediate state violates
        # the constraint.
        if prev_frame_cycles is None or frame_cycles > prev_frame_cycles:
            writes = [
                ("SENS:FRAMEPER", frame_cycles, "Frame period"),
                ("SENS:EXPPER", exposure_cycles, "Exposure"),
            ]
        else:
            writes = [
                ("SENS:EXPPER", exposure_cycles, "Exposure"),
                ("SENS:FRAMEPER", frame_cycles, "Frame period"),
            ]

        previous = {
            "SENS:EXPPER": prev_exposure_cycles,
            "SENS:FRAMEPER": prev_frame_cycles,
        }

        for index, (command, cycles, label) in enumerate(writes):
            if self._send_timing(command, cycles, label):
                continue
            # Only the first write can have landed, so undoing it is enough to
            # put the camera back exactly where it started.
            if index > 0:
                done_command, _, done_label = writes[0]
                restore_to = previous[done_command]
                if restore_to is None:
                    self.print_terminal(
                        f"WARNING: previous {done_label} unknown, cannot roll back"
                    )
                else:
                    self.print_terminal(f"Rolling back {done_label}...")
                    self._send_timing(done_command, restore_to, done_label)
            return False

        self.current_frame_time = cycles_to_sec(frame_cycles)
        self.current_exposure_time = cycles_to_sec(exposure_cycles)
        return True

    def frame_time_for_exposure(self, exposure_s):
        """Frame time that the current mode implies for a given exposure."""
        if self.frame_time_mode is FrameTimeMode.FIXED:
            return self.fixed_frame_time
        return exposure_s + self.frame_time_overhead

    def max_exposure_for_frame_time(self, frame_s):
        """Longest exposure that fits inside a given frame time."""
        return frame_s - MIN_FRAME_OVERHEAD_S

    def set_exposure(self, exposure_s):
        """Set exposure time, deriving the frame period from the current mode.

        In AUTO mode the frame period follows the exposure. In FIXED mode the
        frame period is left alone, and an exposure that will not fit inside it
        is rejected outright rather than silently shortened — a quietly clipped
        integration would corrupt a PTC ramp without any visible symptom.
        """
        frame_s = self.frame_time_for_exposure(exposure_s)

        if self.frame_time_mode is FrameTimeMode.FIXED:
            max_exp = self.max_exposure_for_frame_time(frame_s)
            if exposure_s > max_exp:
                self.print_terminal(
                    f"ERROR: exposure {exposure_s:.6f}s does not fit in the fixed "
                    f"frame time of {frame_s:.6f}s (max {max_exp:.6f}s). "
                    f"Camera unchanged."
                )
                return False

        if not self.apply_timing(exposure_s, frame_s):
            return False

        self._sync_timing_display()
        return True

    def set_frame_time(self, frame_s):
        """Set the frame period explicitly, holding the current exposure.

        Switches the GUI into FIXED mode, since an explicit frame time only has
        meaning if something stops the next exposure change from overwriting it.
        """
        exposure_s = self.exp_input.value()
        max_exp = self.max_exposure_for_frame_time(frame_s)
        if exposure_s > max_exp:
            self.print_terminal(
                f"ERROR: frame time {frame_s:.6f}s is too short for the current "
                f"exposure of {exposure_s:.6f}s (max exposure would be "
                f"{max_exp:.6f}s). Lower the exposure first. Camera unchanged."
            )
            return False

        if not self.apply_timing(exposure_s, frame_s):
            return False

        self.frame_time_mode = FrameTimeMode.FIXED
        self.fixed_frame_time = frame_s
        self._sync_timing_display()
        return True

    def set_frame_time_mode(self, mode, frame_time=None):
        """Switch between AUTO and FIXED frame timing.

        Returns True on success. Switching to FIXED without a frame_time pins
        the frame period at its current value.
        """
        try:
            mode = FrameTimeMode(str(mode).upper())
        except ValueError:
            self.print_terminal(f"ERROR: unknown frame time mode: {mode}")
            return False

        previous_mode = self.frame_time_mode
        previous_fixed = self.fixed_frame_time

        if mode is FrameTimeMode.FIXED:
            if frame_time is None:
                frame_time = self.current_frame_time
            self.frame_time_mode = FrameTimeMode.FIXED
            if not self.set_frame_time(frame_time):
                self.frame_time_mode = previous_mode
                self.fixed_frame_time = previous_fixed
                return False
        else:
            self.frame_time_mode = FrameTimeMode.AUTO
            # Re-apply the current exposure so the frame period catches back up.
            if not self.set_exposure(self.exp_input.value()):
                self.frame_time_mode = previous_mode
                self.fixed_frame_time = previous_fixed
                return False

        self._sync_timing_display()
        self.print_terminal(
            f"Frame time mode: {self.frame_time_mode.value} "
            f"(frame time {self.current_frame_time:.6f}s)"
        )
        return True

    def _sync_timing_display(self):
        """Push current timing state into the GUI widgets without re-triggering."""
        if not hasattr(self, "frame_time_input"):
            return
        self.frame_time_input.blockSignals(True)
        self.frame_time_input.setValue(self.current_frame_time)
        self.frame_time_input.blockSignals(False)
        # Frame time is only directly editable when it is not slaved to exposure.
        self.frame_time_input.setEnabled(self.frame_time_mode is FrameTimeMode.FIXED)

        self.frame_time_mode_dropdown.blockSignals(True)
        self.frame_time_mode_dropdown.setCurrentText(self.frame_time_mode.value)
        self.frame_time_mode_dropdown.blockSignals(False)

    def _begin_timing_wait(self, description):
        """Block captures for three frame periods so the new timing settles.

        Three frame periods of the *applied* frame time, which in FIXED mode is
        unrelated to the exposure — the old code derived this from exposure+0.1
        and would under-wait whenever the frame time was longer than that.
        """
        self.waiting_on_exposure_update = True
        wait_time_sec = 3 * self.current_frame_time
        self.exposure_wait_time = wait_time_sec
        self.exposure_wait_end_time = time.time() + wait_time_sec
        self.capture_button.setEnabled(False)
        self.print_terminal(f"{description} — delaying for {wait_time_sec:.1f} sec")
        QTimer.singleShot(int(wait_time_sec * 1000), self.enable_capture_button)

    def on_exposure_changed(self):
        """Handle exposure time changes from GUI"""
        new_exp = self.exp_input.value()

        # Check if exposure is actually changing
        current_cycles = self._query_cycles("SENS:EXPPER?")
        if current_cycles is not None:
            current_exp_seconds = np.round(cycles_to_sec(current_cycles), 3)

            # If exposure is within 0.001s of current, no need to change. In
            # FIXED mode the frame period is independent, so an unchanged
            # exposure really does mean there is nothing to do.
            if abs(current_exp_seconds - new_exp) < 0.001:
                self.print_terminal(
                    f"Exposure already set to {new_exp}s, no change needed"
                )
                return True

        # Proceed with exposure change
        self.waiting_on_exposure_update = True
        self.exposure_update_start_time = time.time()

        if not self.set_exposure(new_exp):
            self.waiting_on_exposure_update = False
            self.capture_button.setEnabled(True)
            return False

        self._begin_timing_wait(f"Exposure time changed to {new_exp}s")
        return True

    def on_frame_time_changed(self):
        """Handle frame time changes from GUI.

        Deliberately does not share on_exposure_changed's "unchanged, skip"
        guard: the exposure may be identical while the frame time is what moved.
        """
        new_frame = self.frame_time_input.value()

        current_cycles = self._query_cycles("SENS:FRAMEPER?")
        if current_cycles is not None:
            current_frame_seconds = np.round(cycles_to_sec(current_cycles), 3)
            if abs(current_frame_seconds - new_frame) < 0.001:
                self.print_terminal(
                    f"Frame time already set to {new_frame}s, no change needed"
                )
                return True

        self.waiting_on_exposure_update = True
        self.exposure_update_start_time = time.time()

        if not self.set_frame_time(new_frame):
            self.waiting_on_exposure_update = False
            self.capture_button.setEnabled(True)
            # Put the spinbox back to what the camera is actually doing.
            self._sync_timing_display()
            return False

        self._begin_timing_wait(f"Frame time changed to {new_frame}s")
        return True

    def on_frame_time_mode_changed(self, mode_text):
        """Handle AUTO/FIXED dropdown changes from GUI"""
        if not self.set_frame_time_mode(mode_text):
            self._sync_timing_display()
            return False
        self._begin_timing_wait(f"Frame time mode changed to {mode_text}")
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
        """Query camera with automatic serial reconnection on failure"""
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
                # Success - reset error count
                self.serial_error_count = 0
                return values[-1]
            else:
                # No response but no exception - might be communication issue
                self.serial_error_count += 1
                if self.serial_error_count >= self.max_serial_errors:
                    self.print_terminal(
                        f"Serial communication degraded ({self.serial_error_count} consecutive errors). "
                        "Attempting reconnection..."
                    )
                    self.attempt_serial_reconnect()
                return None

        except Exception as e:
            self.serial_error_count += 1
            self.print_terminal(f"Error querying {command}: {e}")

            if self.serial_error_count >= self.max_serial_errors:
                self.print_terminal(
                    f"Multiple serial errors detected ({self.serial_error_count}). "
                    "Attempting reconnection..."
                )
                self.attempt_serial_reconnect()
            return None

    def attempt_serial_reconnect(self):
        """Attempt to reconnect serial communication"""
        try:
            self.print_terminal("Closing existing serial connection...")
            try:
                self.CL.SerialClose()
            except:
                pass  # Ignore errors when closing

            time.sleep(0.5)  # Give port time to release

            self.print_terminal("Re-initializing serial connection...")
            self.setup_serial()

            # Test the connection
            test_response = self.query_scalar("SYS:MODEL?")
            if test_response:
                self.print_terminal("Serial reconnection successful!")
                self.serial_error_count = 0
                self.camera_state = CameraState.READY
            else:
                self.print_terminal(
                    "Serial reconnection failed - no response from camera"
                )
                self.camera_state = CameraState.ERROR

        except Exception as e:
            self.print_terminal(f"Serial reconnection failed: {e}")
            self.camera_state = CameraState.ERROR

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

    # GET ANY COMMAND LINE ARGUMENTS
    args = sys.argv[1:]
    print(f"wsp.py: args = {args}")

    options = "t:"
    long_options = ["trigmode"]

    trigmode = TrigMode.SINGLE  # default

    try:
        # short "-t MODE", long "--trigmode=MODE"
        opts, values = getopt.getopt(args, options, long_options)
        # checking each argument
        print()
        print(f"gui.py: Parsing sys.argv...")
        print(f"gui.py: opts = {opts}")
        print(f"gui.py: values = {values}")
    except getopt.GetoptError as e:
        print(f"Argument error: {e}")
        sys.exit(2)

    for opt, val in opts:
        if opt in ("-t", "--trigmode"):
            val = str(val).strip().upper()
            trigmode = TrigMode[val] if val in TrigMode.__members__ else TrigMode.STREAM

    # validate
    if trigmode not in (TrigMode.SINGLE, TrigMode.STREAM):
        print(f"Unknown trigmode '{trigmode}', falling back to STREAM")
        trigmode = TrigMode.STREAM

    print(f"gui.py: trigmode = {trigmode}")

    app = QApplication(sys.argv)
    viewer = ImageViewer()
    viewer.show()

    # You can disable the server by setting enable_server=False
    # or change the port with server_port=XXXX
    window = SciCamGUI(enable_server=True, server_port=5555, trigmode=trigmode)
    window.viewer = viewer
    window.show()
    sys.exit(app.exec_())
