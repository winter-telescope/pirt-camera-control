# PIRT Camera Control
Control/GUI code for the PIRT 1280SciCam camera. 
Developed originally by Kishalay De for the MIRAGE camera at MDM Observatory and adapted by Nate Lourie for the WINTER-Deep camera at Palomar.

Original GUI is in the mirage_gui directory. Extended GUI with remote client is in the src/camera_control repo.

## Installation + Environment Setup
Instructions here for using conda + pip, but can get your python with venv or however you want.

Try this:

1. conda update -n base -c defaults conda
2. go to top level `pirt-camera-control` directory
3. Make a new conda environment in this directory: `conda create --prefix .conda python=3.12`
4. Activate the new environment in this directory: `conda activate ./.conda`
5. Update pip: `pip install --upgrade pip`
6. Install dependencies: `pip install -e .` Alternately if you want to install the dev dependencies: `pip install -e ".[dev]"`

## Remote command protocol notes

The GUI runs a TCP/IP JSON command server on port 5555 (`src/pirtcam/command_server.py`).
Clients send newline-delimited JSON such as `{"command": "GET_STATUS"}` and get
a newline-delimited JSON reply. Replies go only to the client that sent the
command; `{"event": ...}` notifications (frame saved, capture complete, ...) are
broadcast to every connected client, so several clients (e.g. the WSP camera
daemon and a health probe) can share the port safely.
`pirtcam.client.CameraClient.send_command()` raises `ConnectionError` as soon
as the GUI closes the socket, so callers can reconnect instead of waiting out a
response timeout.

## Behavior when the camera is not responding

The GUI keeps running if the camera is off or the Camera Link serial link
stops answering. It reports `camera_state: "ERROR"` and `camera_connected:
false` in `GET_STATUS`, shows "Camera: NOT CONNECTED" in the window, and
probes the camera with a single query every 10 s instead of running the full
status refresh (which would block the GUI for ~25 s of serial timeouts).
Remote commands that need the camera (`CAPTURE`, `SET_EXPOSURE`,
`SET_FRAME_TIME*`, `SET_TEC_TEMP`, `TEC_EN`, `SET_CORRECTION`,
`SERIAL_COMMAND`) get an immediate error reply while disconnected; `GET_STATUS`
always answers. When the camera answers again the GUI re-reads exposure and
frame period and returns to normal operation by itself. Startup no longer
waits on the camera: the window and command server come up first.

## GUI watchdog (`pirtcam-watchdog`)

`pirtcam-watchdog` keeps the GUI alive on the camera computer. It probes the
GUI with the same `GET_STATUS` command remote clients use and restarts the GUI
when the process is gone (within one probe interval, default 10 s) or when a
running GUI has not answered for `hang_timeout` seconds (default 180 s,
deliberately long because the GUI main thread can stall for tens of seconds
when the camera serial link is unhealthy). A GUI that answers probes is never
touched, even one started by hand, and a GUI whose camera is off is not
restarted either (it answers with `camera_connected: false`).

Nothing site-specific lives in this package: put the launch command, paths and
timings in a JSON file and point the watchdog at it. Example `watchdog.json`,
kept next to your GUI launcher, not in this repository:

```json
{
  "launch": "C:\\Users\\me\\start_gui.bat",
  "host": "localhost",
  "port": 5555,
  "log_dir": "C:\\Users\\me\\watchdog_logs",
  "interval": 10,
  "hang_timeout": 180,
  "startup_grace": 120
}
```

```bash
pirtcam-watchdog --config C:\Users\me\watchdog.json
```

`launch` may be a string (an existing file such as a `.bat` launcher is used
as-is; anything else is split like a command line) or a JSON list of
arguments, which is the safest form for paths containing spaces or
backslashes. Accepted keys: `launch`, `host`, `port`, `interval`,
`probe_timeout`, `hang_timeout`, `startup_grace`, `restart_cooldown`,
`max_restarts_per_hour`, `cwd`, `log_dir`, `pause_file`, `status_file`,
`gui_patterns`. Command-line flags of the same names override the file.

Useful options and files:

- `--dry-run` logs what it would do without killing or launching anything.
  Run this first for a few minutes on a new machine.
- `--once` runs a single probe and exits 0/1.
- Create `<log_dir>/watchdog.pause` to make the watchdog stand down while you
  work on the GUI by hand; delete it to resume.
- `<log_dir>/watchdog.log` is the watchdog's own log, `gui_<timestamp>.log`
  captures the GUI's stdout/stderr for each launch (so a crashing GUI leaves
  its traceback behind), and `watchdog_status.json` is rewritten after every
  probe.
- Restarts are rate limited (`restart_cooldown`, `max_restarts_per_hour`) so a
  GUI that dies on startup cannot be relaunched in a tight loop.

The GUI process is found by its command line (`pirtcam.gui` or
`pirtcam/gui.py`, any path), and restarts kill the whole process tree, so a
launcher script that activates an environment and then runs `gui.py` works
unchanged. On Windows the GUI needs a desktop session, so run the watchdog at
user logon (a Startup-folder shortcut, or a Task Scheduler task triggered "At
log on" set to run only when the user is logged on) and let the watchdog be
the only thing that launches the GUI.

## Workflow for updating tags
Check the current version:
```bash:
python
```
```python:
import pirtcam
print(pirtcam.__version__)
```
Which should print something like: `'1.3.0.dev1+gdbc561fb3'`

Upgrade to tag to the next appropriate version. Here going to 1.3.0 or 1.2.2 is appropriate, the text is saying that we are not yet at v1.3.0. In this case we were previously at v1.2.1.

```bash:
git tag -a v1.3.0 -m "message about the new version"
git push origin v1.3.1
```

## License

This project is licensed under the MIT License.

**Note:** This project depends on [PyQt5](https://www.riverbankcomputing.com/software/pyqt/intro), which is licensed under the GNU General Public License (GPL v3). Users are responsible for complying with that license when installing and using PyQt5.