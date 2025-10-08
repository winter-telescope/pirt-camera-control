import json
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from queue import Empty, Queue
from typing import Callable, Dict, List, Optional


@dataclass
class CaptureProgress:
    """Track progress of a capture sequence"""

    total_frames: int
    completed_frames: int
    start_time: float
    last_frame_time: float
    exposure_time: float
    filenames: List[str]

    @property
    def elapsed_time(self) -> float:
        return time.time() - self.start_time

    @property
    def average_frame_time(self) -> float:
        if self.completed_frames == 0:
            return self.exposure_time + 2.0  # Initial estimate
        return self.elapsed_time / self.completed_frames

    @property
    def estimated_time_remaining(self) -> float:
        remaining_frames = self.total_frames - self.completed_frames
        return remaining_frames * self.average_frame_time

    @property
    def estimated_completion_time(self) -> datetime:
        return datetime.now() + timedelta(seconds=self.estimated_time_remaining)

    @property
    def percent_complete(self) -> float:
        return (
            (self.completed_frames / self.total_frames) * 100
            if self.total_frames > 0
            else 0
        )

    def to_dict(self) -> Dict:
        """Convert to dictionary for easy JSON serialization or plotting"""
        return {
            "total_frames": self.total_frames,
            "completed_frames": self.completed_frames,
            "percent_complete": self.percent_complete,
            "elapsed_time": self.elapsed_time,
            "average_frame_time": self.average_frame_time,
            "estimated_time_remaining": self.estimated_time_remaining,
            "estimated_completion": self.estimated_completion_time.isoformat(),
            "exposure_time": self.exposure_time,
            "filenames": self.filenames,
        }


class CameraClient:
    def __init__(self, host="localhost", port=5555):
        self.host = host
        self.port = port
        self.socket = None
        self.receive_thread = None
        self.response_queue = Queue()
        self.notification_queue = Queue()
        self.running = False
        self.current_capture: Optional[CaptureProgress] = None
        self.capture_history: List[CaptureProgress] = []
        self.progress_callbacks: List[Callable[[CaptureProgress], None]] = []

        # Detect if we're in Jupyter/IPython
        self.in_jupyter = self._detect_jupyter()

    def _detect_jupyter(self) -> bool:
        """Detect if running in Jupyter notebook"""
        try:
            from IPython import get_ipython

            ipython = get_ipython()
            if ipython is not None:
                return "IPKernelApp" in ipython.config
        except ImportError:
            pass
        return False

    def connect(self):
        """Connect to the camera GUI server"""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.host, self.port))
        self.socket.settimeout(0.1)  # Non-blocking receive
        print(f"Connected to camera server at {self.host}:{self.port}")

        # Start receive thread
        self.running = True
        self.receive_thread = threading.Thread(target=self._receive_loop)
        self.receive_thread.daemon = True
        self.receive_thread.start()

    def disconnect(self):
        """Disconnect from the server"""
        self.running = False
        if self.receive_thread:
            self.receive_thread.join(timeout=1)
        if self.socket:
            self.socket.close()
            self.socket = None

    def add_progress_callback(self, callback: Callable[[CaptureProgress], None]):
        """Add a callback to be called when capture progress updates"""
        self.progress_callbacks.append(callback)

    def remove_progress_callback(self, callback: Callable[[CaptureProgress], None]):
        """Remove a progress callback"""
        if callback in self.progress_callbacks:
            self.progress_callbacks.remove(callback)

    def _receive_loop(self):
        """Continuously receive messages from server"""
        buffer = ""
        while self.running:
            try:
                data = self.socket.recv(4096).decode("utf-8")
                if not data:
                    break

                buffer += data
                # Process complete lines
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        try:
                            msg = json.loads(line)
                            # Route message to appropriate queue
                            if "event" in msg:
                                self.notification_queue.put(msg)
                                # Update progress if it's a frame saved event
                                if (
                                    msg.get("event") == "frame_saved"
                                    and self.current_capture
                                ):
                                    self._update_capture_progress(msg)
                            else:
                                self.response_queue.put(msg)
                        except json.JSONDecodeError:
                            print(f"Failed to parse: {line}")

            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"Receive error: {e}")
                break

    def _update_capture_progress(self, notification: Dict):
        """Update current capture progress"""
        if self.current_capture:
            if notification.get("event") == "frame_saved":
                self.current_capture.completed_frames += 1
                self.current_capture.last_frame_time = time.time()
                self.current_capture.filenames.append(notification.get("filename", ""))
                if hasattr(self, "_debug") and self._debug:
                    print(
                        f"[DEBUG] Frame {self.current_capture.completed_frames}/{self.current_capture.total_frames} saved"
                    )
            elif notification.get("event") == "stack_saved":
                # For stacks, mark all frames as complete
                self.current_capture.completed_frames = (
                    self.current_capture.total_frames
                )
                self.current_capture.last_frame_time = time.time()
                self.current_capture.filenames = [notification.get("filename", "")]
                if hasattr(self, "_debug") and self._debug:
                    print(
                        f"[DEBUG] Stack saved with {notification.get('frames', 0)} frames"
                    )

            # Call progress callbacks
            for callback in self.progress_callbacks:
                try:
                    callback(self.current_capture)
                except Exception as e:
                    print(f"Progress callback error: {e}")

    def send_command(self, command_dict):
        """Send a command and wait for response"""
        if not self.socket:
            raise Exception("Not connected to server")

        # Clear response queue
        while not self.response_queue.empty():
            self.response_queue.get()

        # Send command
        command_json = json.dumps(command_dict)
        if hasattr(self, "_debug") and self._debug:
            print(f"[CLIENT DEBUG] Sending: {command_json}")
        self.socket.send(command_json.encode("utf-8"))

        # Wait for primary response
        try:
            response = self.response_queue.get(
                timeout=120.0
            )  # Increased timeout for long waits
            if hasattr(self, "_debug") and self._debug:
                print(f"[CLIENT DEBUG] Received: {response}")
            return response
        except Empty:
            return {"status": "error", "message": "Response timeout"}

    def capture_frames(
        self,
        nframes=1,
        wait_for_completion=True,
        show_progress=True,
        headers=None,
        stack=False,
        filename=None,
        debug=False,
    ):
        """
        Capture one or more frames

        Args:
            nframes: Number of frames to capture
            wait_for_completion: If True, block until all frames are captured
            show_progress: If True, print progress updates to console
            headers: Custom FITS headers in one of these formats:
                - Dict: {'KEYWORD': value} or {'KEYWORD': (value, 'comment')}
                - List of tuples: [('KEYWORD', value), ('KEYWORD', value, 'comment')]
                - List of astropy Card objects
            stack: If True, save all frames as a single FITS cube file
            filename: Custom filename for the capture (without path)
            debug: If True, print debug information

        Returns:
            Dict with status and captured filenames
        """
        if debug:
            print(f"[DEBUG] Starting capture of {nframes} frames")
            print(f"[DEBUG] Stack mode: {stack}")
            if filename:
                print(f"[DEBUG] Custom filename: {filename}")

        # Get current exposure time for progress estimation
        status = self.get_status()
        exposure_time = 1.0  # default
        if status.get("status") == "success":
            exposure_time = float(status["data"].get("exposure", 1.0))

        # Initialize capture progress
        self.current_capture = CaptureProgress(
            total_frames=nframes,
            completed_frames=0,
            start_time=time.time(),
            last_frame_time=time.time(),
            exposure_time=exposure_time,
            filenames=[],
        )

        # Prepare command
        command = {"command": "CAPTURE", "nframes": nframes}

        # Add optional parameters
        if headers is not None:
            # Convert headers to a format that can be JSON serialized
            if isinstance(headers, dict):
                command["headers"] = headers
            elif isinstance(headers, list):
                # Convert list format to dict
                headers_dict = {}
                for item in headers:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        key = str(item[0])
                        if len(item) == 2:
                            headers_dict[key] = item[1]
                        else:
                            headers_dict[key] = [item[1], item[2]]
                    elif hasattr(item, "keyword"):  # Astropy Card
                        headers_dict[item.keyword] = [
                            item.value,
                            getattr(item, "comment", ""),
                        ]
                command["headers"] = headers_dict

        if stack:
            command["stack"] = True

        if filename:
            command["filename"] = filename

        # Send capture command
        response = self.send_command(command)

        if debug:
            print(f"[DEBUG] Server response: {response}")

        if response.get("status") != "success":
            self.current_capture = None
            return response

        if wait_for_completion:
            # Store debug flag for other methods
            self._debug = debug
            self._stack_mode = stack

            # Clear notification queue before starting
            while not self.notification_queue.empty():
                try:
                    self.notification_queue.get_nowait()
                except Empty:
                    break

            # Wait for all frames to be captured
            last_update = time.time()
            no_progress_time = 0
            last_completed = 0
            expected_time = (
                (self.current_capture.exposure_time + 2.0) * nframes if stack else 0
            )

            if debug:
                print(f"[DEBUG] Waiting for {nframes} frames to complete...")
                print(
                    f"[DEBUG] Current capture: {self.current_capture.completed_frames}/{self.current_capture.total_frames}"
                )
                if stack:
                    print(
                        f"[DEBUG] Stack mode: Progress updates will only appear after all frames are captured"
                    )
                    print(
                        f"[DEBUG] Estimated time for stack capture: {expected_time:.1f}s"
                    )

            while self.current_capture.completed_frames < nframes:
                # Process all pending notifications
                notifications_processed = 0
                while True:
                    try:
                        notif = self.notification_queue.get_nowait()
                        if debug:
                            print(f"[DEBUG] Got notification: {notif}")
                        # Process notification manually if needed
                        if notif.get("event") in ["frame_saved", "stack_saved"]:
                            # The _receive_loop should have already called _update_capture_progress
                            # but let's make sure
                            if self.current_capture.completed_frames < nframes:
                                self._update_capture_progress(notif)
                        notifications_processed += 1
                    except Empty:
                        break

                # Show progress every second if enabled
                current_time = time.time()
                if show_progress and current_time - last_update > 1.0:
                    if (
                        not debug and not stack
                    ):  # Don't show progress bar in debug mode or stack mode
                        self._print_progress()
                    elif stack and not debug:
                        # For stack mode, show elapsed time instead of frame progress
                        elapsed = self.current_capture.elapsed_time
                        print(
                            f"\rCapturing stack: {elapsed:.1f}s elapsed...",
                            end="",
                            flush=True,
                        )
                    last_update = current_time

                # Check for stalled progress
                if self.current_capture.completed_frames == last_completed:
                    no_progress_time += 0.1
                    if (
                        debug
                        and no_progress_time > 5
                        and int(no_progress_time) % 5 == 0
                    ):
                        print(
                            f"[DEBUG] No progress for {int(no_progress_time)}s. "
                            f"Completed: {self.current_capture.completed_frames}/{nframes}"
                        )
                        if stack and no_progress_time < expected_time:
                            print(
                                f"[DEBUG] This is normal for stack mode - waiting for all frames to complete"
                            )
                else:
                    no_progress_time = 0
                    last_completed = self.current_capture.completed_frames
                    if debug:
                        print(
                            f"[DEBUG] Progress: {self.current_capture.completed_frames}/{nframes} frames completed"
                        )

                # Timeout if no progress for 30 seconds (but longer for stacks)

                timeout_threshold = max(300, expected_time * 1.5) if stack else 30
                if no_progress_time > timeout_threshold:
                    print(
                        f"\nWARNING: No progress for {int(no_progress_time)}s. Completed {self.current_capture.completed_frames}/{nframes} frames"
                    )
                    if debug:
                        print(
                            f"[DEBUG] Breaking due to no progress timeout (threshold: {timeout_threshold}s)"
                        )
                        print(f"[DEBUG] Final state: {self.current_capture.to_dict()}")
                    # break # Uncomment to break on timeout

                # Check for overall timeout (5 minutes per frame max, or expected time * 2 for stacks)
                overall_timeout = (
                    max(nframes * 300, expected_time * 2) if stack else nframes * 300
                )
                if self.current_capture.elapsed_time > overall_timeout:
                    print("\nERROR: Overall capture timeout exceeded!")
                    if debug:
                        print(f"[DEBUG] Breaking due to overall timeout")
                        print(
                            f"[DEBUG] Elapsed time: {self.current_capture.elapsed_time}s (timeout: {overall_timeout}s)"
                        )
                    # break # Uncomment to break on timeout

                # Small sleep to prevent CPU spinning
                time.sleep(0.1)

            # Final status
            if debug:
                print(
                    f"[DEBUG] Capture loop ended. Final count: {self.current_capture.completed_frames}/{nframes}"
                )
                print(
                    f"[DEBUG] Filenames captured: {len(self.current_capture.filenames)}"
                )

            # Final progress update
            if show_progress and not debug:
                self._print_progress()
                print()  # New line after progress

            # Clean up debug flag
            self._debug = False
            self._stack_mode = False

            # Save to history
            self.capture_history.append(self.current_capture)

            # Return results
            result = {
                "status": "success",
                "frames_captured": self.current_capture.completed_frames,
                "filenames": self.current_capture.filenames,
                "elapsed_time": self.current_capture.elapsed_time,
                "average_frame_time": self.current_capture.average_frame_time,
                "stack_mode": stack,
            }

    def capture_frames_jupyter(
        self, nframes=1, headers=None, stack=False, filename=None
    ):
        """
        Jupyter-friendly version of capture_frames with widget progress bar

        Args:
            nframes: Number of frames to capture
            headers: Custom FITS headers
            stack: If True, save all frames as a single FITS cube file
            filename: Custom filename for the capture

        Returns:
            Dict with status and captured filenames
        """
        try:
            from IPython.display import display
            from ipywidgets import HTML, HBox, IntProgress, VBox

            # Create progress widgets
            progress_bar = IntProgress(
                value=0,
                min=0,
                max=nframes,
                description="Capturing:",
                bar_style="info",
                orientation="horizontal",
            )

            status_text = HTML(value=f"<b>Starting capture of {nframes} frames...</b>")
            progress_text = HTML(value="0%")

            # Display widgets
            progress_display = HBox([progress_bar, progress_text])
            display(VBox([status_text, progress_display]))

            # Define progress callback
            def update_widget(capture_progress):
                progress_bar.value = capture_progress.completed_frames
                progress_text.value = f"{capture_progress.percent_complete:.1f}%"
                status_text.value = (
                    f"<b>Captured {capture_progress.completed_frames}/{capture_progress.total_frames} frames</b> - "
                    f"ETA: {capture_progress.estimated_time_remaining:.0f}s"
                )

            # Add callback
            self.add_progress_callback(update_widget)

            try:
                # Run capture without console progress
                result = self.capture_frames(
                    nframes=nframes,
                    headers=headers,
                    stack=stack,
                    filename=filename,
                    show_progress=False,  # Disable console progress
                    wait_for_completion=True,
                    debug=False,
                )

                # Update final status
                if result.get("status") == "success":
                    status_text.value = f'<b style="color:green">✓ Capture complete! {result["frames_captured"]} frames in {result["elapsed_time"]:.1f}s</b>'
                    progress_bar.bar_style = "success"
                else:
                    status_text.value = f'<b style="color:red">✗ Capture failed: {result.get("message", "Unknown error")}</b>'
                    progress_bar.bar_style = "danger"

                return result

            finally:
                # Remove callback
                self.remove_progress_callback(update_widget)

        except ImportError:
            # Fallback to regular capture if ipywidgets not available
            print("ipywidgets not available, using standard capture")
            return self.capture_frames(
                nframes=nframes,
                headers=headers,
                stack=stack,
                filename=filename,
                show_progress=True,
                wait_for_completion=True,
            )

    def _print_progress(self):
        """Print current capture progress"""
        if not self.current_capture:
            return

        prog = self.current_capture

        # For stack mode, show different progress since we don't get frame updates
        if hasattr(self, "_stack_mode") and self._stack_mode:
            elapsed = prog.elapsed_time
            estimated_total = (
                prog.exposure_time * prog.total_frames + 5.0
            )  # Add overhead
            percent = (
                min(100, (elapsed / estimated_total) * 100)
                if estimated_total > 0
                else 0
            )

            progress_str = f"Stack capture in progress: {elapsed:.1f}s elapsed (~{percent:.0f}% estimated)"

            if self.in_jupyter:
                # In Jupyter, use IPython display to update in place
                try:
                    from IPython.display import clear_output

                    clear_output(wait=True)
                    print(progress_str)
                except ImportError:
                    print(progress_str)
            else:
                # Clear the line with spaces before printing
                print(f"\r{' ' * 80}\r{progress_str}", end="", flush=True)
        else:
            # Regular frame-by-frame progress
            bar_length = 30
            filled = int(bar_length * prog.completed_frames / prog.total_frames)
            bar = "█" * filled + "░" * (bar_length - filled)

            progress_str = (
                f"Capture Progress: [{bar}] {prog.completed_frames}/{prog.total_frames} "
                f"({prog.percent_complete:.1f}%) - "
                f"ETA: {prog.estimated_time_remaining:.0f}s "
                f"(~{prog.estimated_completion_time.strftime('%H:%M:%S')})"
            )

            if self.in_jupyter:
                # In Jupyter, use IPython display to update in place
                try:
                    from IPython.display import clear_output

                    clear_output(wait=True)
                    print(progress_str)
                except ImportError:
                    # Fallback for Jupyter without IPython.display
                    print(f"\r{progress_str}", end="", flush=True)
            else:
                # Regular terminal - clear line with spaces then print
                # Calculate the length of the progress string to clear properly
                terminal_width = 80  # Default terminal width
                try:
                    import shutil

                    terminal_width = shutil.get_terminal_size().columns
                except:
                    pass

                # Clear the line and print progress
                print(
                    f"\r{' ' * min(terminal_width - 1, len(progress_str) + 10)}\r{progress_str}",
                    end="",
                    flush=True,
                )

    def get_capture_status(self) -> Optional[Dict]:
        """Get current capture status"""
        if self.current_capture:
            return self.current_capture.to_dict()
        return None

    def get_capture_history(self) -> List[Dict]:
        """Get history of all captures in this session"""
        return [cap.to_dict() for cap in self.capture_history]

    def wait_for_current_capture(self, timeout=None):
        """Wait for current capture to complete"""
        if not self.current_capture:
            return

        start_time = time.time()
        while (
            self.current_capture
            and self.current_capture.completed_frames
            < self.current_capture.total_frames
        ):
            if timeout and (time.time() - start_time) > timeout:
                break
            time.sleep(0.1)

    def set_exposure(self, exposure_seconds, wait=True, debug=False):
        """
        Set exposure time in seconds

        Args:
            exposure_seconds: Exposure time in seconds
            wait: If True, wait for exposure to settle before returning
            debug: If True, print debug information
        """
        self._debug = debug
        if debug:
            print(
                f"[CLIENT DEBUG] set_exposure called with {exposure_seconds}s, wait={wait}"
            )

        result = self.send_command(
            {"command": "SET_EXPOSURE", "exposure": exposure_seconds, "wait": wait}
        )

        self._debug = False
        return result

    def set_tec_temperature(self, temp):
        """Set TEC temperature (-20, -40, or -60)"""
        return self.send_command({"command": "SET_TEC_TEMP", "temperature": temp})

    def set_object_name(self, object_name):
        """Set object name for FITS header"""
        return self.send_command({"command": "SET_OBJECT", "object": object_name})

    def set_observer_name(self, observer_name):
        """Set observer name for FITS header"""
        return self.send_command({"command": "SET_OBSERVER", "observer": observer_name})

    def set_save_path(self, path):
        """
        Set save path for FITS files

        Args:
            path: Directory path (can use ~ for home directory)
        """
        return self.send_command({"command": "SET_PATH", "path": path})

    def set_filename(self, filename):
        """
        Set custom filename for next capture

        Args:
            filename: Custom filename (without path). Set to None or empty string to use default naming.
        """
        return self.send_command({"command": "SET_FILENAME", "filename": filename})

    def get_status(self):
        """Get current camera status"""
        return self.send_command({"command": "GET_STATUS"})

    def set_correction(self, correction_type, value):
        """Set correction parameters (GAIN/OFFSET/SUB, ON/OFF)"""
        return self.send_command(
            {"command": "SET_CORRECTION", "type": correction_type, "value": value}
        )

    def is_exposure_updating(self):
        """Check if exposure is currently being updated"""
        status = self.get_status()
        if status.get("status") == "success":
            data = status.get("data", {})
            # Check if capture button is disabled (indicates exposure updating)
            return not data.get("tec_locked", True) or "delaying" in data.get(
                "status", ""
            )
        return False

    def wait_for_exposure(self, timeout=60):
        """Wait for exposure update to complete"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if not self.is_exposure_updating():
                return True
            time.sleep(1)
        return False


# Example usage
if __name__ == "__main__":
    # Example 1: Simple capture with custom headers
    client = CameraClient("localhost", 5555)

    try:
        client.connect()
        print("Connected to camera server\n")

        # Capture with custom headers - dictionary format
        print("Capturing with custom headers (dict format)...")
        headers = {
            "AIRMASS": 1.25,
            "FILTER": ("V", "Johnson V filter"),
            "TELESCOP": "MDM 2.4m",
            "INSTRUME": "MIRAGE",
        }
        result = client.capture_frames(5, headers=headers)
        print(f"Captured {result['frames_captured']} frames\n")

        # Capture with custom headers - tuple format
        print("Capturing with custom headers (tuple format)...")
        headers = [
            ("RA", 123.456, "Right Ascension (degrees)"),
            ("DEC", 45.678, "Declination (degrees)"),
            ("EPOCH", 2000.0),
            ("FOCUS", 1234),
        ]
        result = client.capture_frames(5, headers=headers)
        print(f"Captured {result['frames_captured']} frames\n")

        # Capture as stack
        print("Capturing 10 frames as a stack...")
        result = client.capture_frames(
            10, stack=True, headers={"STACKID": "test_stack_001"}
        )
        print(f"Saved stack with {result['frames_captured']} frames")
        print(
            f"Stack file: {result['filenames'][0] if result['filenames'] else 'Unknown'}\n"
        )

    finally:
        client.disconnect()


# Example 2: Multi-exposure sequence with headers and stacks
def multi_exposure_with_headers():
    """Take multiple sets of frames with custom headers"""
    client = CameraClient("localhost", 5555)

    # Define capture sequence with headers
    sequence = [
        {
            "name": "DARK_1s",
            "exposure": 1.0,
            "nframes": 100,
            "stack": True,
            "headers": {
                "IMAGETYP": ("DARK", "Image type"),
                "FILTER": ("NONE", "No filter for darks"),
            },
        },
        {
            "name": "FLAT_V_5s",
            "exposure": 5.0,
            "nframes": 50,
            "stack": False,
            "headers": {
                "IMAGETYP": ("FLAT", "Image type"),
                "FILTER": ("V", "Johnson V filter"),
                "FLATTYP": ("DOME", "Flat field type"),
            },
        },
        {
            "name": "NGC1234_V_60s",
            "exposure": 60.0,
            "nframes": 20,
            "stack": True,
            "headers": {
                "IMAGETYP": ("LIGHT", "Image type"),
                "FILTER": ("V", "Johnson V filter"),
                "RA": (123.456, "Right Ascension J2000"),
                "DEC": (45.678, "Declination J2000"),
                "EPOCH": 2000.0,
                "EQUINOX": 2000.0,
            },
        },
    ]

    try:
        client.connect()
        print("Starting multi-exposure sequence with headers\n")

        total_start = time.time()

        for config in sequence:
            print(f"\n{'='*60}")
            print(
                f"Capturing {config['name']}: {config['nframes']} frames at {config['exposure']}s"
            )
            print(f"Mode: {'Stack' if config['stack'] else 'Individual files'}")
            print(f"{'='*60}")

            # Set parameters
            client.set_object_name(config["name"])
            client.set_exposure(config["exposure"])
            time.sleep(2)  # Wait for exposure to settle

            # Capture frames with headers
            result = client.capture_frames(
                config["nframes"],
                headers=config["headers"],
                stack=config["stack"],
                wait_for_completion=True,
                show_progress=True,
            )

            print(f"\nCompleted {config['name']}: {result['frames_captured']} frames")
            if config["stack"]:
                print(
                    f"Stack saved as: {result['filenames'][0] if result['filenames'] else 'Unknown'}"
                )
            else:
                print(f"Saved {len(result['filenames'])} individual files")

        total_elapsed = time.time() - total_start
        print(f"\n{'='*60}")
        print(f"Sequence complete! Total time: {total_elapsed:.1f} seconds")
        print(f"{'='*60}")

    finally:
        client.disconnect()


# Example 3: Astropy integration
def capture_with_astropy_headers():
    """Example using astropy Card objects for headers"""
    from astropy.io import fits

    client = CameraClient("localhost", 5555)

    try:
        client.connect()

        # Create astropy Card objects
        cards = [
            fits.Card("OBSERVER", "J. Smith", "Observer name"),
            fits.Card("PROPOSID", "MDM-2024A-001", "Proposal ID"),
            fits.Card("PROGID", "SN2024abc", "Program ID"),
            fits.Card("WEATHER", "CLEAR", "Weather conditions"),
            fits.Card("SEEING", 1.2, "Seeing in arcseconds"),
        ]

        # You can also mix formats
        mixed_headers = cards + [
            ("HUMIDITY", 45.5, "Relative humidity (%)"),
            ("WINDSPD", 5.2, "Wind speed (m/s)"),
        ]

        print("Capturing with astropy Card headers...")
        result = client.capture_frames(10, headers=mixed_headers)
        print(f"Captured {result['frames_captured']} frames with custom headers")

    finally:
        client.disconnect()


# Example 4: Calibration sequence with stacks
def calibration_sequence():
    """Complete calibration sequence saving as stacks"""
    client = CameraClient("localhost", 5555)

    try:
        client.connect()
        print("Starting calibration sequence (all saved as stacks)\n")

        # Take bias frames
        print("Taking bias frames...")
        client.set_object_name("BIAS")
        client.set_exposure(0.001)  # Minimum exposure
        time.sleep(2)
        result = client.capture_frames(
            100,
            stack=True,
            headers={"IMAGETYP": ("BIAS", "Image type")},
            show_progress=True,
        )
        bias_file = result["filenames"][0] if result["filenames"] else None
        print(f"Bias stack: {bias_file}\n")

        # Take darks at multiple exposures
        dark_exposures = [1, 5, 10, 30, 60, 120]
        dark_files = {}

        for exp in dark_exposures:
            print(f"\nTaking {exp}s dark frames...")
            client.set_object_name(f"DARK_{exp}s")
            client.set_exposure(exp)
            time.sleep(2)

            result = client.capture_frames(
                50,
                stack=True,
                headers={"IMAGETYP": ("DARK", "Image type"), "EXPTIME": exp},
                show_progress=True,
            )
            dark_files[exp] = result["filenames"][0] if result["filenames"] else None
            print(f"Dark stack ({exp}s): {dark_files[exp]}")

        # Take flats for each filter
        filters = ["U", "B", "V", "R", "I"]
        flat_files = {}

        for filt in filters:
            print(f"\nTaking flat frames for {filt} filter...")
            client.set_object_name(f"FLAT_{filt}")
            client.set_exposure(5.0)  # Adjust for proper counts
            time.sleep(2)

            result = client.capture_frames(
                50,
                stack=True,
                headers={
                    "IMAGETYP": ("FLAT", "Image type"),
                    "FILTER": (filt, f"{filt} band filter"),
                },
                show_progress=True,
            )
            flat_files[filt] = result["filenames"][0] if result["filenames"] else None
            print(f"Flat stack ({filt}): {flat_files[filt]}")

        # Summary
        print("\n" + "=" * 60)
        print("Calibration sequence complete!")
        print("=" * 60)
        print(f"Bias: {bias_file}")
        print("Darks:")
        for exp, filename in dark_files.items():
            print(f"  {exp}s: {filename}")
        print("Flats:")
        for filt, filename in flat_files.items():
            print(f"  {filt}: {filename}")

    finally:
        client.disconnect()


if __name__ == "__main__":
    # Run the multi-exposure example with headers
    multi_exposure_with_headers()
