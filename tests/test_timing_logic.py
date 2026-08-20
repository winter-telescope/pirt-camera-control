"""Exercise SciCamGUI timing logic against a mock camera.

gui.py can't be imported off-instrument (BitFlow DLLs, Windows-only os call),
so we stub the missing pieces and call the timing methods unbound with a mock
self. This tests the real shipped code, not a reimplementation of it.
"""

import itertools
import os
import sys
import types

# --- stub the Windows/instrument-only bits so gui.py imports ---------------
os.add_dll_directory = lambda path: None

def stub_module(name):
    """A module whose ordinary attributes resolve to placeholders.

    Dunder attributes are left alone so that libraries which introspect
    modules (astropy does) don't choke on them.
    """
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

from pirtcam.gui import (  # noqa: E402
    MIN_FRAME_OVERHEAD_S,
    FrameTimeMode,
    SciCamGUI,
    cycles_to_sec,
    sec_to_cycles,
)


class MockCamera:
    """Enforces the one firmware rule we care about: frame period >= exposure."""

    def __init__(self, exposure_cycles, frame_cycles):
        self.exposure_cycles = exposure_cycles
        self.frame_cycles = frame_cycles
        self.violations = []
        self.log = []

    def write(self, register, cycles):
        if register == "SENS:EXPPER":
            if cycles > self.frame_cycles:
                self.violations.append(
                    f"exposure {cycles} > frame {self.frame_cycles}"
                )
                return "Out of Range"
            self.exposure_cycles = cycles
        elif register == "SENS:FRAMEPER":
            if cycles < self.exposure_cycles:
                self.violations.append(
                    f"frame {cycles} < exposure {self.exposure_cycles}"
                )
                return "Out of Range"
            self.frame_cycles = cycles
        self.log.append((register, cycles))
        return "OK"


class MockGUI:
    """Minimal stand-in for SciCamGUI, wired to a MockCamera."""

    def __init__(self, camera):
        self.camera = camera
        self.terminal = []
        self.current_frame_time = 0.0
        self.current_exposure_time = 0.0
        self.frame_time_mode = FrameTimeMode.AUTO
        self.frame_time_overhead = 0.1
        self.fixed_frame_time = 0.0

    def print_terminal(self, msg):
        self.terminal.append(msg)

    def query_scalar(self, command):
        return {
            "SENS:EXPPER?": str(self.camera.exposure_cycles),
            "SENS:FRAMEPER?": str(self.camera.frame_cycles),
        }[command]

    def send_command(self, command):
        register, value = command.rsplit(" ", 1)
        return self.camera.write(register, int(value))

    # bind the real implementations
    _query_cycles = SciCamGUI._query_cycles
    _send_timing = SciCamGUI._send_timing
    apply_timing = SciCamGUI.apply_timing
    frame_time_for_exposure = SciCamGUI.frame_time_for_exposure
    max_exposure_for_frame_time = SciCamGUI.max_exposure_for_frame_time


def check(start_exp, start_frame, new_exp, new_frame):
    cam = MockCamera(sec_to_cycles(start_exp), sec_to_cycles(start_frame))
    gui = MockGUI(cam)
    ok = gui.apply_timing(new_exp, new_frame)

    problems = []
    if cam.violations:
        problems.append(f"constraint violated mid-transition: {cam.violations}")
    if ok:
        got_exp = cycles_to_sec(cam.exposure_cycles)
        got_frame = cycles_to_sec(cam.frame_cycles)
        if abs(got_exp - new_exp) > 1e-6:
            problems.append(f"exposure landed at {got_exp}, wanted {new_exp}")
        if abs(got_frame - new_frame) > 1e-6:
            problems.append(f"frame landed at {got_frame}, wanted {new_frame}")
    return ok, problems


def main():
    # (exposure, frame) states covering AUTO-style and FIXED-style timing
    states = [
        (0.01, 0.11),   # short auto
        (1.0, 1.1),     # nominal auto
        (5.0, 5.1),     # long auto
        (0.01, 10.0),   # fixed frame, tiny exposure  (PTC ramp start)
        (5.0, 10.0),    # fixed frame, long exposure  (PTC ramp end)
        (9.9, 10.0),    # fixed frame, exposure at the limit
        (0.5, 30.0),    # slow fixed cadence
    ]

    failures = 0
    transitions = 0
    for (e0, f0), (e1, f1) in itertools.product(states, repeat=2):
        transitions += 1
        ok, problems = check(e0, f0, e1, f1)
        if not ok:
            failures += 1
            print(f"REJECTED {e0}/{f0} -> {e1}/{f1}")
        if problems:
            failures += 1
            print(f"FAIL {e0}/{f0} -> {e1}/{f1}: {problems}")

    print(f"\n{transitions} transitions exercised, {failures} failures")

    # A frame time that cannot fit the exposure must be refused outright,
    # leaving the camera untouched.
    cam = MockCamera(sec_to_cycles(1.0), sec_to_cycles(1.1))
    gui = MockGUI(cam)
    before = (cam.exposure_cycles, cam.frame_cycles)
    rejected = not gui.apply_timing(5.0, 5.0 + MIN_FRAME_OVERHEAD_S / 2)
    untouched = (cam.exposure_cycles, cam.frame_cycles) == before
    print(f"too-short frame time rejected: {rejected}, camera untouched: {untouched}")
    if not (rejected and untouched):
        failures += 1

    # AUTO vs FIXED frame time derivation
    gui.frame_time_mode = FrameTimeMode.AUTO
    auto = gui.frame_time_for_exposure(2.0)
    gui.frame_time_mode = FrameTimeMode.FIXED
    gui.fixed_frame_time = 10.0
    fixed_short = gui.frame_time_for_exposure(2.0)
    fixed_long = gui.frame_time_for_exposure(0.01)
    print(f"AUTO(2.0s exp) -> {auto}s frame  (expect 2.1)")
    print(f"FIXED(2.0s exp) -> {fixed_short}s frame  (expect 10.0)")
    print(f"FIXED(0.01s exp) -> {fixed_long}s frame  (expect 10.0)")
    if not (auto == 2.1 and fixed_short == 10.0 and fixed_long == 10.0):
        failures += 1

    print("\nRESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
