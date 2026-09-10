"""
pirtcam-watchdog: keep the PIRT camera GUI alive on the camera computer.

The watchdog checks the GUI's health the same way a remote client does: it
connects to the GUI's TCP/IP JSON command server and sends
``{"command": "GET_STATUS"}``. A reply means the GUI process is up *and* its
Qt main thread is servicing commands. The watchdog restarts the GUI when

* no GUI process is running at all (fast: within one probe interval), or
* a GUI process exists but has not answered for ``hang_timeout`` seconds
  (slow on purpose: the GUI main thread can legitimately stall for tens of
  seconds while the camera serial link misbehaves).

A GUI whose *camera* is off is not restarted: the GUI keeps answering
GET_STATUS with ``camera_connected: false`` and handles that itself.

It never touches a GUI that answers probes, including one started by hand
before the watchdog came up. Restarts are rate limited so a GUI that crashes
on startup cannot be relaunched in a tight loop.

Site-specific settings (launch command, paths, timings) come from a JSON
config file given with ``--config``; nothing site-specific lives in this
package. Example ``watchdog.json``::

    {
      "launch": "C:\\\\Users\\\\me\\\\start_gui.bat",
      "host": "localhost",
      "port": 5555,
      "log_dir": "C:\\\\Users\\\\me\\\\watchdog_logs",
      "interval": 10,
      "hang_timeout": 180,
      "startup_grace": 120
    }

Then, at user logon (the GUI needs a desktop session)::

    pirtcam-watchdog --config C:\\Users\\me\\watchdog.json

Command-line options override the config file. Create the pause file
(``<log_dir>/watchdog.pause`` by default) to make the watchdog stand down
while working on the GUI by hand; delete it to resume. ``watchdog.log``,
one ``gui_<timestamp>.log`` per launch (GUI stdout/stderr) and
``watchdog_status.json`` (rewritten after every probe) land in ``log_dir``.
"""

import argparse
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None

log = logging.getLogger("pirtcam.watchdog")

# Substrings that identify a GUI process on its command line. Matches both
# ``python -m pirtcam.gui`` and ``python .../pirtcam/gui.py`` (any OS path).
DEFAULT_GUI_PATTERNS = ("pirtcam.gui", "pirtcam/gui.py", "pirtcam\\gui.py")


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------
@dataclass
class ProbeResult:
    ok: bool
    detail: str = ""
    latency_s: float = 0.0
    data: Optional[dict] = None


def probe_gui(host="localhost", port=5555, timeout=10.0):
    """Ask the GUI for its status over the command protocol.

    Opens a short-lived connection, sends GET_STATUS and waits for any
    JSON reply that is not an event notification. Any well-formed reply
    counts as "alive": even an error dict proves the GUI thread is
    servicing commands.
    """
    t0 = time.monotonic()
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall((json.dumps({"command": "GET_STATUS"}) + "\n").encode("utf-8"))

        buffer = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                return ProbeResult(False, "server closed connection before replying")
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and "event" in msg:
                    continue  # unsolicited notification, keep waiting
                latency = time.monotonic() - t0
                data = msg.get("data") if isinstance(msg, dict) else None
                return ProbeResult(True, "reply received", latency, data)
        return ProbeResult(False, f"no reply within {timeout:.0f}s")
    except (OSError, ConnectionError) as e:
        return ProbeResult(False, f"connect/send failed: {e}")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Process discovery / control
# ---------------------------------------------------------------------------
def find_gui_processes(patterns=DEFAULT_GUI_PATTERNS):
    """Return psutil.Process objects whose command line looks like the GUI."""
    if psutil is None:
        raise RuntimeError("psutil is required for process discovery")
    me = os.getpid()
    found = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid == me:
                continue
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(cmdline)
            if any(p in joined for p in patterns):
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


def kill_processes(procs, grace_s=10.0):
    """Terminate then kill the given processes and their children."""
    if not procs:
        return
    victims = []
    for proc in procs:
        try:
            victims.extend(proc.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        victims.append(proc)
    for proc in victims:
        try:
            log.info("terminating pid %s (%s)", proc.pid, " ".join(proc.cmdline())[:120])
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(victims, timeout=grace_s)
    for proc in alive:
        try:
            log.warning("pid %s did not exit, killing", proc.pid)
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(alive, timeout=grace_s)


def launch_process(cmd, cwd=None, stdout_path=None):
    """Start the GUI. stdout/stderr go to ``stdout_path`` if given."""
    kwargs = {}
    if cwd:
        kwargs["cwd"] = cwd
    if stdout_path:
        fh = open(stdout_path, "a", buffering=1)
        kwargs["stdout"] = fh
        kwargs["stderr"] = subprocess.STDOUT
    if os.name == "nt":
        # New process group so Ctrl-C in the watchdog console does not
        # propagate into the GUI.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(cmd, **kwargs)
    log.info("launched %s as pid %s", cmd, proc.pid)
    return proc


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------
@dataclass
class SupervisorConfig:
    host: str = "localhost"
    port: int = 5555
    interval_s: float = 10.0  # time between probes
    probe_timeout_s: float = 10.0
    hang_timeout_s: float = 180.0  # unresponsive-but-alive before restart
    startup_grace_s: float = 120.0  # after a launch, don't judge responsiveness
    restart_cooldown_s: float = 60.0  # minimum spacing between restarts
    max_restarts_per_hour: int = 12  # beyond this, back off to slow_interval
    slow_interval_s: float = 600.0
    launch_cmd: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    log_dir: Optional[str] = None
    pause_file: Optional[str] = None
    status_file: Optional[str] = None
    gui_patterns: tuple = DEFAULT_GUI_PATTERNS
    dry_run: bool = False


@dataclass
class SupervisorState:
    last_probe_ok: Optional[bool] = None
    last_probe_detail: str = ""
    last_ok_time: Optional[float] = None
    unhealthy_since: Optional[float] = None
    last_launch_time: Optional[float] = None
    restart_times: List[float] = field(default_factory=list)
    restarts_total: int = 0
    last_restart_reason: str = ""
    paused: bool = False
    gui_pids: List[int] = field(default_factory=list)
    camera_connected: Optional[bool] = None


class GuiSupervisor:
    """Decision logic for the watchdog. All side effects are injectable so
    the policy can be unit tested with fakes."""

    def __init__(
        self,
        config: SupervisorConfig,
        probe_fn: Optional[Callable[[], ProbeResult]] = None,
        find_procs_fn: Optional[Callable[[], list]] = None,
        kill_fn: Optional[Callable[[list], None]] = None,
        launch_fn: Optional[Callable[[], object]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = config
        self.state = SupervisorState()
        self.clock = clock
        self.sleep = sleep
        self._child = None
        self._probe_fn = probe_fn or (
            lambda: probe_gui(config.host, config.port, config.probe_timeout_s)
        )
        self._find_procs_fn = find_procs_fn or (
            lambda: find_gui_processes(config.gui_patterns)
        )
        self._kill_fn = kill_fn or kill_processes
        self._launch_fn = launch_fn or self._default_launch
        self._stop = False

    # -- side effects ----------------------------------------------------
    def _default_launch(self):
        if not self.cfg.launch_cmd:
            raise RuntimeError("no launch command configured")
        stdout_path = None
        if self.cfg.log_dir:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            stdout_path = os.path.join(self.cfg.log_dir, f"gui_{stamp}.log")
        return launch_process(self.cfg.launch_cmd, self.cfg.cwd, stdout_path)

    def _is_paused(self):
        return bool(self.cfg.pause_file and os.path.exists(self.cfg.pause_file))

    def _restarts_last_hour(self, now):
        cutoff = now - 3600.0
        self.state.restart_times = [t for t in self.state.restart_times if t > cutoff]
        return len(self.state.restart_times)

    def _write_status(self, now):
        if not self.cfg.status_file:
            return
        payload = asdict(self.state)
        payload["wall_time"] = time.time()
        payload["restarts_last_hour"] = self._restarts_last_hour(now)
        try:
            tmp = self.cfg.status_file + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(payload, fh, indent=2, default=str)
            os.replace(tmp, self.cfg.status_file)
        except OSError as e:  # never let bookkeeping kill the watchdog
            log.debug("could not write status file: %s", e)

    # -- policy ----------------------------------------------------------
    def restart(self, reason, procs):
        now = self.clock()
        st = self.state
        if st.last_launch_time is not None and (
            now - st.last_launch_time < self.cfg.restart_cooldown_s
        ):
            log.info(
                "restart wanted (%s) but last launch was %.0fs ago; waiting for cooldown",
                reason,
                now - st.last_launch_time,
            )
            return False

        log.warning("RESTARTING GUI: %s", reason)
        st.last_restart_reason = reason
        if self.cfg.dry_run:
            log.warning(
                "dry run: would kill %s and launch %s",
                [p.pid for p in procs],
                self.cfg.launch_cmd,
            )
        else:
            if procs:
                self._kill_fn(procs)
            self._child = self._launch_fn()
        st.last_launch_time = now
        st.unhealthy_since = None
        st.restart_times.append(now)
        st.restarts_total += 1
        return True

    def run_once(self):
        """One probe/decide/act cycle. Returns the ProbeResult (None if paused)."""
        now = self.clock()
        st = self.state

        st.paused = self._is_paused()
        if st.paused:
            log.info("paused (pause file present); not probing")
            self._write_status(now)
            return None

        procs = self._find_procs_fn()
        st.gui_pids = [p.pid for p in procs]
        result = self._probe_fn()
        first_probe = st.last_probe_ok is None
        was_unhealthy = st.unhealthy_since is not None
        st.last_probe_ok = result.ok
        st.last_probe_detail = result.detail
        if result.ok and isinstance(result.data, dict):
            st.camera_connected = result.data.get("camera_connected")

        if result.ok:
            if first_probe:
                log.info(
                    "GUI healthy (pids %s), %.2fs latency, camera_connected=%s",
                    st.gui_pids, result.latency_s, st.camera_connected,
                )
            elif was_unhealthy:
                log.info("GUI healthy again (pids %s)", st.gui_pids)
            st.last_ok_time = now
            st.unhealthy_since = None
            self._write_status(now)
            return result

        # probe failed
        if st.unhealthy_since is None:
            st.unhealthy_since = now
            log.warning("GUI probe failed: %s (pids %s)", result.detail, st.gui_pids)

        in_grace = (
            st.last_launch_time is not None
            and now - st.last_launch_time < self.cfg.startup_grace_s
        )

        if not procs:
            if in_grace and self._child is not None and self._child.poll() is None:
                # We just launched it and it is still coming up (the launcher
                # may be a wrapper whose child is not visible yet).
                log.info("GUI process not found yet, still within startup grace")
            else:
                self.restart("no GUI process running", procs)
        else:
            down_for = now - st.unhealthy_since
            if in_grace:
                log.info("GUI unresponsive for %.0fs but within startup grace", down_for)
            elif down_for >= self.cfg.hang_timeout_s:
                self.restart(
                    f"GUI unresponsive for {down_for:.0f}s (pids {st.gui_pids})", procs
                )
            else:
                log.info(
                    "GUI unresponsive for %.0fs (restart after %.0fs)",
                    down_for,
                    self.cfg.hang_timeout_s,
                )

        self._write_status(now)
        return result

    def stop(self):
        self._stop = True

    def run_forever(self):
        log.info(
            "watchdog started: probing %s:%s every %.0fs, launch=%s",
            self.cfg.host, self.cfg.port, self.cfg.interval_s, self.cfg.launch_cmd,
        )
        while not self._stop:
            try:
                self.run_once()
            except Exception:  # keep the watchdog alive no matter what
                log.exception("unexpected error in watchdog cycle")
            now = self.clock()
            if self._restarts_last_hour(now) >= self.cfg.max_restarts_per_hour:
                log.error(
                    "%d restarts in the last hour; backing off to %.0fs between probes",
                    self._restarts_last_hour(now),
                    self.cfg.slow_interval_s,
                )
                self.sleep(self.cfg.slow_interval_s)
            else:
                self.sleep(self.cfg.interval_s)


# ---------------------------------------------------------------------------
# Configuration / CLI
# ---------------------------------------------------------------------------
# JSON config keys (all optional). CLI flags of the same name override them.
CONFIG_KEYS = (
    "launch", "host", "port", "interval", "probe_timeout", "hang_timeout",
    "startup_grace", "restart_cooldown", "max_restarts_per_hour", "cwd",
    "log_dir", "pause_file", "status_file", "gui_patterns",
)


def default_launch_cmd():
    return [sys.executable, "-m", "pirtcam.gui"]


def build_parser():
    p = argparse.ArgumentParser(
        prog="pirtcam-watchdog",
        description="Keep the PIRT camera GUI alive by probing its command server.",
    )
    p.add_argument("--config", default=None, help="JSON config file (see module docstring)")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--interval", type=float, default=None, help="seconds between probes")
    p.add_argument("--probe-timeout", type=float, default=None)
    p.add_argument(
        "--hang-timeout",
        type=float,
        default=None,
        help="restart if a running GUI stays unresponsive this long (s)",
    )
    p.add_argument("--startup-grace", type=float, default=None)
    p.add_argument("--restart-cooldown", type=float, default=None)
    p.add_argument("--max-restarts-per-hour", type=int, default=None)
    p.add_argument(
        "--launch",
        default=None,
        help='GUI launch command, e.g. "C:\\\\path\\\\start_gui.bat" or '
        '"python -m pirtcam.gui -t SINGLE" (default: current interpreter, -m pirtcam.gui)',
    )
    p.add_argument("--cwd", default=None, help="working directory for the GUI")
    p.add_argument("--log-dir", default=None, help="watchdog + GUI log directory")
    p.add_argument("--pause-file", default=None)
    p.add_argument("--status-file", default=None)
    p.add_argument("--dry-run", action="store_true", help="log decisions, never kill/launch")
    p.add_argument("--once", action="store_true", help="single probe cycle, then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def load_config_file(path):
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a JSON object")
    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        raise ValueError(f"{path}: unknown keys {unknown}; known keys: {list(CONFIG_KEYS)}")
    return data


def _split_launch(value):
    """Turn the ``launch`` setting into an argv list.

    A JSON list is used verbatim (safest for paths with spaces or
    backslashes). A string naming an existing file is a single-element
    command; any other string is split like a shell command line
    (non-POSIX rules on Windows, so backslashes survive there).
    """
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    value = str(value)
    if os.path.exists(value):
        return [value]
    return shlex.split(value, posix=(os.name != "nt"))


def config_from_args(args):
    """Merge config file (if any) and CLI flags into a SupervisorConfig."""
    file_cfg = load_config_file(args.config) if args.config else {}

    def pick(name, default):
        cli = getattr(args, name, None)
        if cli is not None:
            return cli
        return file_cfg.get(name, default)

    launch = pick("launch", None)
    launch_cmd = _split_launch(launch) if launch else default_launch_cmd()

    log_dir = pick("log_dir", None)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    pause_file = pick("pause_file", None) or (
        os.path.join(log_dir, "watchdog.pause") if log_dir else None
    )
    status_file = pick("status_file", None) or (
        os.path.join(log_dir, "watchdog_status.json") if log_dir else None
    )
    patterns = file_cfg.get("gui_patterns") or DEFAULT_GUI_PATTERNS

    return SupervisorConfig(
        host=pick("host", "localhost"),
        port=int(pick("port", 5555)),
        interval_s=float(pick("interval", 10.0)),
        probe_timeout_s=float(pick("probe_timeout", 10.0)),
        hang_timeout_s=float(pick("hang_timeout", 180.0)),
        startup_grace_s=float(pick("startup_grace", 120.0)),
        restart_cooldown_s=float(pick("restart_cooldown", 60.0)),
        max_restarts_per_hour=int(pick("max_restarts_per_hour", 12)),
        launch_cmd=launch_cmd,
        cwd=pick("cwd", None),
        log_dir=log_dir,
        pause_file=pause_file,
        status_file=status_file,
        gui_patterns=tuple(patterns),
        dry_run=bool(args.dry_run),
    )


def setup_logging(log_dir, verbose):
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [logging.StreamHandler()]
    if log_dir:
        handlers.append(logging.FileHandler(os.path.join(log_dir, "watchdog.log")))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    setup_logging(cfg.log_dir, args.verbose)
    sup = GuiSupervisor(cfg)
    if args.once:
        result = sup.run_once()
        if result is None:
            print("paused")
            return 0
        print(json.dumps(asdict(result), indent=2, default=str))
        return 0 if result.ok else 1
    try:
        sup.run_forever()
    except KeyboardInterrupt:
        log.info("watchdog stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
