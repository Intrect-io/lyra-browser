"""``lyra-browser vnc``: a persistent VNC display for hosts with no screen of their own.

A command for the person at the keyboard, never a tool for the model. It writes two
systemd user units — an ``Xvfb`` display and an ``x11vnc`` server bound to it — with a
fixed display, port and password, and records them in a sidecar (``vnc.json``). The
MCP server never starts or stops them: it only notices a live display from that
sidecar (``config._persistent_display``) and opens its window there, headful and
attended, so the user can watch and take over from a VNC viewer.

The password is kept in plain text, mode 0600, in ``<vnc dir>/passwd``: an
``-rfbauth`` file is no better (x11vnc's own help: "NOT encrypted, only obscured with
a fixed key"), and ``-passwdfile read:`` keeps it off every command line and lets
``reset-password`` take effect without a restart. RFB itself is unencrypted, so the
default listen address is loopback; reach it over SSH or a private network.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import (
    VNC_SIDECAR_NAME,
    X11_SOCKET_DIR,
    display_alive,
    display_number,
    read_vnc_sidecar,
    vnc_dir,
)

XVFB_UNIT = "lyra-xvfb.service"
X11VNC_UNIT = "lyra-x11vnc.service"
PASSWORD_NAME = "passwd"
# RFB's VNC authentication uses the first 8 characters only (a DES key).
PASSWORD_LENGTH = 8
_PASSWORD_ALPHABET = string.ascii_letters + string.digits

DEFAULT_DISPLAY = ":98"
DEFAULT_PORT = 5902
DEFAULT_LISTEN = "127.0.0.1"
DEFAULT_SCREEN = "1920x1080x24"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


@dataclass
class Host:
    """What the commands touch, so tests can swap every piece of it."""

    vnc_dir: Path
    unit_dir: Path
    # Factories, not class attributes: a plain function stored on the class would read
    # as a method to anyone (or any analyser) who forgets the instance attribute wins.
    run: Runner = field(default_factory=lambda: _run)
    which: Callable[[str], str | None] = field(default_factory=lambda: shutil.which)
    platform: str = sys.platform

    @classmethod
    def default(cls) -> Host:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return cls(vnc_dir=vnc_dir(), unit_dir=Path(base).expanduser() / "systemd" / "user")

    @property
    def password_file(self) -> Path:
        return self.vnc_dir / PASSWORD_NAME

    @property
    def sidecar_file(self) -> Path:
        return self.vnc_dir / VNC_SIDECAR_NAME

    def systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return self.run(["systemctl", "--user", *args])


@dataclass(frozen=True)
class Settings:
    display: str = DEFAULT_DISPLAY
    port: int = DEFAULT_PORT
    listen: str = DEFAULT_LISTEN
    screen: str = DEFAULT_SCREEN


def generate_password() -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def write_password(path: Path, password: str) -> None:
    """Write ``password`` readable by its owner only, replacing the file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(password + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_password(path: Path) -> str | None:
    try:
        line = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return line or None


def xvfb_command(xvfb: str, settings: Settings) -> list[str]:
    """The X server: a framebuffer nobody sees except through VNC. No TCP listener."""
    return [xvfb, settings.display, "-screen", "0", settings.screen, "-nolisten", "tcp"]


def x11vnc_command(x11vnc: str, settings: Settings, password_file: Path) -> list[str]:
    """The VNC server on that display: shared (agent and user at once), kept up."""
    return [
        x11vnc,
        "-display",
        settings.display,
        "-rfbport",
        str(settings.port),
        "-listen",
        settings.listen,
        "-passwdfile",
        f"read:{password_file}",
        "-forever",
        "-shared",
        "-noxdamage",
        "-quiet",
    ]


def _exec_line(cmd: Sequence[str]) -> str:
    return " ".join(_quote_systemd(part) for part in cmd)


def _quote_systemd(part: str) -> str:
    if part and all(c not in part for c in " \t\"'\\;$%"):
        return part
    return '"' + part.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_units(xvfb: str, x11vnc: str, settings: Settings, password_file: Path) -> dict:
    """The two unit files, by name.

    x11vnc is bound to the display (``BindsTo``) and wanted by it, so a restarted
    Xvfb brings its VNC server back with it; it waits for the X socket before it
    starts, which is where the hand-started pair used to fail ("XOpenDisplay
    failed"). ``Restart=always`` with no start limit also covers a listen address
    whose interface (a VPN, say) comes up after the user manager does.
    """
    number = display_number(settings.display)
    sock = X11_SOCKET_DIR / f"X{number}"
    # "$$" is a literal "$" to systemd, which would otherwise expand "$i" itself.
    wait_for_socket = (
        f"/bin/sh -c 'i=0; while [ ! -S {sock} ]; do i=$$((i+1)); "
        "[ $$i -gt 100 ] && exit 1; sleep 0.1; done'"
    )
    xvfb_unit = f"""\
# Written by `lyra-browser vnc install`; `lyra-browser vnc uninstall` removes it.
[Unit]
Description=lyra-browser persistent X display {settings.display}

[Service]
ExecStart={_exec_line(xvfb_command(xvfb, settings))}
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
"""
    vnc_unit = f"""\
# Written by `lyra-browser vnc install`; `lyra-browser vnc uninstall` removes it.
[Unit]
Description=lyra-browser VNC server for display {settings.display}
BindsTo={XVFB_UNIT}
After={XVFB_UNIT}
StartLimitIntervalSec=0

[Service]
ExecStartPre={wait_for_socket}
ExecStart={_exec_line(x11vnc_command(x11vnc, settings, password_file))}
Restart=always
RestartSec=5

[Install]
WantedBy={XVFB_UNIT} default.target
"""
    return {XVFB_UNIT: xvfb_unit, X11VNC_UNIT: vnc_unit}


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    probe = "::1" if probe == "::" else probe
    try:
        with socket.create_connection((probe, port), timeout=timeout) as conn:
            return conn.recv(12).startswith(b"RFB ")
    except OSError:
        return False


def _wait(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def _err(msg: str) -> None:
    print(f"lyra-browser vnc: {msg}", file=sys.stderr)


def _settings_from(args: argparse.Namespace) -> Settings | None:
    if display_number(args.display) is None:
        _err(f"--display must be a local display like :98, not {args.display!r}")
        return None
    if not 1 <= args.port <= 65535:
        _err(f"--port out of range: {args.port}")
        return None
    return Settings(display=args.display, port=args.port, listen=args.listen, screen=args.screen)


def _linger_on(host: Host) -> bool | None:
    user = os.environ.get("USER") or ""
    out = host.run(["loginctl", "show-user", user, "-p", "Linger"])
    if out.returncode != 0:
        return None
    return out.stdout.strip() == "Linger=yes"


def cmd_install(args: argparse.Namespace, host: Host) -> int:
    if not host.platform.startswith("linux"):
        _err("a persistent X display is a Linux thing; macOS and Windows open windows as is")
        return 2
    settings = _settings_from(args)
    if settings is None:
        return 2
    xvfb, x11vnc = host.which("Xvfb"), host.which("x11vnc")
    missing = [name for name, path in (("Xvfb", xvfb), ("x11vnc", x11vnc)) if not path]
    if missing:
        _err(
            f"{' and '.join(missing)} not found. Install them first "
            "(Debian/Ubuntu: sudo apt install xvfb x11vnc)."
        )
        return 2
    assert xvfb and x11vnc

    previous = read_vnc_sidecar(host.vnc_dir)
    new_password = read_password(host.password_file) is None
    if new_password:
        write_password(host.password_file, generate_password())

    host.unit_dir.mkdir(parents=True, exist_ok=True)
    for name, text in render_units(xvfb, x11vnc, settings, host.password_file).items():
        (host.unit_dir / name).write_text(text, encoding="utf-8")

    sidecar = {
        "display": settings.display,
        "port": settings.port,
        "listen": settings.listen,
        "screen": settings.screen,
        "units": [XVFB_UNIT, X11VNC_UNIT],
        "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    host.vnc_dir.mkdir(parents=True, exist_ok=True)
    tmp = host.sidecar_file.with_name(VNC_SIDECAR_NAME + ".tmp")
    tmp.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, host.sidecar_file)

    # A unit of the same name started by hand (systemd-run) and left failed would
    # shadow the file just written until it is cleared.
    host.systemctl("reset-failed", X11VNC_UNIT, XVFB_UNIT)
    steps = [
        ("daemon-reload",),
        ("enable", XVFB_UNIT, X11VNC_UNIT),
        # restart, not start: a re-install with other flags must take effect now.
        ("restart", XVFB_UNIT),
        ("restart", X11VNC_UNIT),
    ]
    for step in steps:
        out = host.systemctl(*step)
        if out.returncode != 0:
            _err(f"systemctl --user {' '.join(step)} failed: {out.stderr.strip()}")
            return 1

    up = _wait(lambda: display_alive(settings.display), args.wait) and _wait(
        lambda: _port_open(settings.listen, settings.port), args.wait
    )
    print(f"display:  {settings.display} ({settings.screen})")
    print(f"vnc:      {settings.listen}:{settings.port}")
    if new_password:
        print(f"password: {read_password(host.password_file)}  (kept; see `vnc password`)")
    else:
        print("password: unchanged (`lyra-browser vnc password` prints it)")
    if previous and previous.get("display") != settings.display:
        print(f"note:     the display moved from {previous.get('display')} to {settings.display}")
    if _linger_on(host) is False:
        print(
            "warning:  lingering is off, so the display stops when you log out: "
            "loginctl enable-linger $USER"
        )
    if not up:
        _err(
            "the units were installed but the display or the VNC port is not answering yet; "
            f"see `lyra-browser vnc status` and journalctl --user -u {X11VNC_UNIT}"
        )
        return 1
    print("lyra opens its window there whenever it runs with no display of its own.")
    return 0


def _unit_state(host: Host, unit: str) -> str:
    out = host.systemctl("is-active", unit)
    return out.stdout.strip() or "unknown"


def cmd_status(args: argparse.Namespace, host: Host) -> int:
    sidecar = read_vnc_sidecar(host.vnc_dir)
    if sidecar is None:
        print("not installed (lyra-browser vnc install)")
        return 1
    display = sidecar["display"]
    listen, port = sidecar.get("listen", DEFAULT_LISTEN), int(sidecar.get("port", DEFAULT_PORT))
    report = {
        "display": display,
        "screen": sidecar.get("screen"),
        "vnc": f"{listen}:{port}",
        XVFB_UNIT: _unit_state(host, XVFB_UNIT),
        X11VNC_UNIT: _unit_state(host, X11VNC_UNIT),
        "x_socket": display_alive(display),
        "rfb_answers": _port_open(listen, port),
        "password_file": str(host.password_file)
        + ("" if host.password_file.exists() else " (missing)"),
        "installed_at": sidecar.get("installed_at"),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for key, value in report.items():
            print(f"{key + ':':24} {value}")
    healthy = report["x_socket"] and report["rfb_answers"]
    return 0 if healthy else 1


def cmd_password(args: argparse.Namespace, host: Host) -> int:
    password = read_password(host.password_file)
    if password is None:
        _err("no password yet (lyra-browser vnc install)")
        return 1
    print(password)
    return 0


def cmd_reset_password(args: argparse.Namespace, host: Host) -> int:
    if args.stdin:
        password = sys.stdin.readline().strip()
        if not password:
            _err("no password on stdin")
            return 2
        if len(password) > PASSWORD_LENGTH:
            _err(f"VNC uses only the first {PASSWORD_LENGTH} characters of a password")
    else:
        password = generate_password()
    write_password(host.password_file, password)
    # x11vnc reads the file again for each new client ("read:"), so no restart.
    print(password if not args.stdin else "password updated")
    return 0


def cmd_uninstall(args: argparse.Namespace, host: Host) -> int:
    host.systemctl("disable", "--now", X11VNC_UNIT, XVFB_UNIT)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    trash = host.vnc_dir / "trash" / stamp
    moved = []
    for path in (host.unit_dir / XVFB_UNIT, host.unit_dir / X11VNC_UNIT, host.sidecar_file):
        if path.exists():
            trash.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), trash / path.name)
            moved.append(path.name)
    host.systemctl("daemon-reload")
    if not moved:
        print("nothing installed")
        return 0
    print(f"stopped and removed; the old files are in {trash}")
    print(f"the password is kept in {host.password_file} for a later install")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lyra-browser vnc",
        description=(
            "Keep a VNC display up on a host with no screen, so lyra's window opens there "
            "and you can watch and take over from a VNC viewer."
        ),
    )
    # Accepted and ignored, so the plugin launcher may always pass --client.
    parser.add_argument("--client", default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    inst = sub.add_parser("install", help="Write, enable and start the systemd user units.")
    inst.add_argument("--display", default=DEFAULT_DISPLAY, help="X display (default :98).")
    inst.add_argument("--port", type=int, default=DEFAULT_PORT, help="VNC port (default 5902).")
    inst.add_argument(
        "--listen",
        default=DEFAULT_LISTEN,
        help=(
            "Address the VNC server listens on (default 127.0.0.1). RFB is unencrypted: "
            "use loopback with an SSH tunnel, or a private network's address."
        ),
    )
    inst.add_argument(
        "--screen", default=DEFAULT_SCREEN, help="Screen WxHxDEPTH (default 1920x1080x24)."
    )
    inst.add_argument("--wait", type=float, default=10.0, help="Seconds to wait for it to answer.")

    stat = sub.add_parser("status", help="Is the display up and does VNC answer?")
    stat.add_argument("--json", action="store_true")
    sub.add_parser("password", help="Print the VNC password.")
    reset = sub.add_parser("reset-password", help="Pick a new VNC password.")
    reset.add_argument("--stdin", action="store_true", help="Read the new password from stdin.")
    sub.add_parser("uninstall", help="Stop and remove the units (keeps the password).")
    return parser


def main(argv: list[str] | None = None, host: Host | None = None) -> int:
    args = build_parser().parse_args(argv)
    commands = {
        "install": cmd_install,
        "status": cmd_status,
        "password": cmd_password,
        "reset-password": cmd_reset_password,
        "uninstall": cmd_uninstall,
    }
    return commands[args.command](args, host or Host.default())


__all__ = [
    "DEFAULT_DISPLAY",
    "DEFAULT_PORT",
    "Host",
    "Settings",
    "main",
    "render_units",
    "x11vnc_command",
    "xvfb_command",
]
