# Original version
import os
import sys

# Add required DLL directories for BitFlow SDK and Camera Link Serial driver
os.add_dll_directory(r"C:\BitFlow SDK 6.5\Bin64")
os.add_dll_directory(r"C:\Program Files\CameraLink\Serial")

import time
from datetime import datetime, timezone

import BFModule.BufferAcquisition as Buf
import BFModule.CLComm as CLCom
import numpy as np
from astropy.io import fits
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QImage, QPixmap
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


class ImageViewer(QWidget):
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MIRAGE Quick Look Viewer")
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

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MIRAGE Control Panel")
        self.setGeometry(100, 100, 600, 500)
        self.setup_ui()
        self.setup_serial()

        self.exp_input.blockSignals(True)
        self.exp_input.setValue(1.0)  # GUI default only, no serial write
        self.exp_input.blockSignals(False)

        self.exp_input.setValue(1.0)  # default to 1 second

        self.update_status_indicators()
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.update_status_indicators)
        self.status_timer.start(5000)

        # Disable capture button until TEC is locked
        self.capture_button.setEnabled(False)

    def update_status_indicators(self):
        setpoint = self.query_scalar("TEMP:SENS:SET?")
        if setpoint:
            self.tec_setpoint_label.setText(f"Setpoint (°C): {setpoint}")
        temp = self.query_scalar("TEMP:SENS?")
        if temp:
            self.dynamic_temp_label.setText(f"Temp (°C): {temp}")
        soc = self.query_scalar("SOC?")
        if soc:
            self.soc_label.setText(f"Loaded SOC: {soc}")

        gain = self.query_scalar("CORR:GAIN?")
        if gain:
            self.gaincor_dropdown.setCurrentText(gain.strip().upper())

        off = self.query_scalar("CORR:OFFSET?")
        if off:
            self.offcor_dropdown.setCurrentText(off.strip().upper())

        sub = self.query_scalar("CORR:SUB?")
        if sub:
            self.subcor_dropdown.setCurrentText(sub.strip().upper())

        tec_lock = self.query_scalar("TEC:LOCK?")
        if tec_lock and tec_lock.strip().upper() == "ON":
            self.tec_lock_light.setStyleSheet(
                "background-color: green; border-radius: 8px;"
            )
            self.capture_button.setEnabled(True)
        else:
            self.tec_lock_light.setStyleSheet(
                "background-color: red; border-radius: 8px;"
            )
            self.capture_button.setEnabled(False)
        soc = self.query_scalar("SOC?")

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
        layout.addWidget(self.exp_input)

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
        default_folder = os.getcwd()
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
        folder = QFileDialog.getExistingDirectory(
            self, "Select Save Folder", os.getcwd()
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
        return output

    def set_exposure(self, exposure_s):
        CLOCK_FREQ = 15.0
        exposure_cycles = int((exposure_s * 1e6 * 1000) / (1e3 / CLOCK_FREQ))
        exposure_cycles = max(12, min(exposure_cycles, 4294967142))
        print(f"Setting exposure to {exposure_s}s ({exposure_cycles} cycles)")
        self.send_command(f"SENS:EXPPER {exposure_cycles}")

        frame_cycles = int(((exposure_s + 0.1) * 1e6 * 1000) / (1e3 / CLOCK_FREQ))
        frame_cycles = max(1800, min(frame_cycles, 4294967295))
        self.send_command(f"SENS:FRAMEPER {frame_cycles}")

    def handle_tec_temp_change(self, target):
        try:
            target = float(target)
            set_cmd = self.query_scalar("TEMP:SENS:SET?")
            current = float(set_cmd) if set_cmd else None
            if current is None:
                self.print_terminal("Could not read current TEC setpoint.")
                return

            if abs(current + 60.0) < 1.0 and target > -60:
                for step in [-55, -50, -45]:
                    if step > target:
                        break
                    self.print_terminal(f"Warming up: setting TEC to {step}°C")
                    self.send_command(f"TEMP:SENS:SET {step}")
                    self.wait_for_tec_lock(timeout_sec=300)
            self.print_terminal(f"Final TEC setpoint: {target}°C")
            self.send_command(f"TEMP:SENS:SET {target}")
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

    def on_exposure_changed(self):
        self.waiting_on_exposure_update = True
        new_exp = self.exp_input.value()
        CLOCK_FREQ = 15.0
        exposure_cycles = int((new_exp * 1e6 * 1000) / (1e3 / CLOCK_FREQ))
        exposure_cycles = max(12, min(exposure_cycles, 4294967142))
        self.send_command(f"SENS:EXPPER {exposure_cycles}")

        frame_cycles = int(((new_exp + 0.1) * 1e6 * 1000) / (1e3 / CLOCK_FREQ))
        frame_cycles = max(1800, min(frame_cycles, 4294967295))
        self.send_command(f"SENS:FRAMEPER {frame_cycles}")

        wait_time_sec = (3 * frame_cycles) / (CLOCK_FREQ * 1e6)
        self.capture_button.setEnabled(False)
        self.print_terminal(
            f"Exposure time changed — delaying for {wait_time_sec:.1f} sec"
        )
        QTimer.singleShot(int(wait_time_sec * 1000), self.enable_capture_button)

    def enable_capture_button(self):
        self.capture_button.setEnabled(True)
        self.waiting_on_exposure_update = False

    def capture_frame(self):
        if self.waiting_on_exposure_update:
            self.print_terminal("Waiting for exposure update delay to finish...")
            return

        try:
            nframes = int(self.nframes_input.text().strip())
        except ValueError:
            self.print_terminal("Invalid number of frames; defaulting to 1")
            nframes = 1

        self.CL.SerialClose()

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
            filename = os.path.join(folder, "scicam_" + curtime + ".fits")

            hdu = fits.PrimaryHDU(img)
            hdr = hdu.header
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

            hdu.writeto(filename, overwrite=True)
            self.CL.SerialClose()
            self.status_label.setText(f"Saved: {filename}")
            if hasattr(self, "viewer") and self.viewer:
                self.viewer.update_image(img)

            if i == nframes - 1:
                self.setup_serial()
                time.sleep(0.2)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    viewer = ImageViewer()
    viewer.show()

    window = SciCamGUI()
    window.viewer = viewer
    window.show()
    sys.exit(app.exec_())
