"""The persistent VNC display: how the server finds it, and what `vnc install` writes.

The server side runs on a fake X socket dir and a fake sidecar; the CLI side on a
fake ``systemctl`` runner, so nothing here starts an X server or touches the host's
systemd. ``scripts/verify_vnc_e2e.py`` is the real-Xvfb, real-x11vnc, real-Chrome gate.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from lyra_browser import config as config_mod
from lyra_browser import vnc_cli
from lyra_browser.config import Config, display_alive, display_number, read_vnc_sidecar
from lyra_browser.server import instructions_for
from lyra_browser.session import BrowserSession, _is_missing_x_server, launch_kwargs

# --------------------------------------------------------------------------
# Fixtures: a host with no display of its own, a fake X socket dir, a sidecar
# --------------------------------------------------------------------------


@pytest.fixture
def no_display(monkeypatch, tmp_path):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("LYRA_BROWSER_HEADLESS", raising=False)
    monkeypatch.setenv("LYRA_BROWSER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("sys.platform", "linux")
    sockets = tmp_path / "x11"
    sockets.mkdir()
    monkeypatch.setattr(config_mod, "X11_SOCKET_DIR", sockets)
    vnc = tmp_path / "vnc"
    monkeypatch.setenv("LYRA_BROWSER_VNC_DIR", str(vnc))
    return sockets, vnc


def _install_sidecar(vnc: Path, display=":98", listen="127.0.0.1", port=5902) -> None:
    vnc.mkdir(parents=True, exist_ok=True)
    (vnc / "vnc.json").write_text(
        json.dumps({"display": display, "listen": listen, "port": port, "screen": "1920x1080x24"})
    )


def _x_up(sockets: Path, number=98) -> None:
    (sockets / f"X{number}").touch()


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_display_number_parses_local_displays_only():
    assert display_number(":98") == 98
    assert display_number(":0.0") == 0
    assert display_number("host:1") is None
    assert display_number("wayland-0") is None


def test_live_persistent_display_makes_claude_code_attended(no_display):
    sockets, vnc = no_display
    _install_sidecar(vnc)
    _x_up(sockets)

    cfg = Config.from_env(client="claude-code")

    assert cfg.display == ":98"
    assert cfg.headless is False and cfg.attended is True
    assert cfg.watch == {"vnc": "127.0.0.1:5902", "display": ":98"}


def test_generic_uses_it_too(no_display):
    sockets, vnc = no_display
    _install_sidecar(vnc)
    _x_up(sockets)
    assert Config.from_env(client="generic").attended is True


def test_sidecar_without_a_live_x_server_stays_headless(no_display):
    _sockets, vnc = no_display
    _install_sidecar(vnc)

    cfg = Config.from_env(client="claude-code")

    assert cfg.display is None and cfg.headless is True and cfg.watch is None


def test_no_sidecar_stays_headless(no_display):
    sockets, _vnc = no_display
    _x_up(sockets)
    cfg = Config.from_env(client="claude-code")
    assert cfg.display is None and cfg.headless is True


@pytest.mark.parametrize("client", ["hermes", "remote"])
def test_hermes_and_remote_never_take_it(no_display, client):
    sockets, vnc = no_display
    _install_sidecar(vnc)
    _x_up(sockets)
    cfg = Config.from_env(client=client)
    assert cfg.display is None and cfg.headless is True


def test_an_inherited_display_wins(no_display, monkeypatch):
    sockets, vnc = no_display
    _install_sidecar(vnc)
    _x_up(sockets)
    monkeypatch.setenv("DISPLAY", ":0")

    cfg = Config.from_env(client="claude-code")

    assert cfg.display is None and cfg.headless is False and cfg.watch is None


def test_headless_env_beats_the_display(no_display, monkeypatch):
    sockets, vnc = no_display
    _install_sidecar(vnc)
    _x_up(sockets)
    monkeypatch.setenv("LYRA_BROWSER_HEADLESS", "1")

    cfg = Config.from_env(client="claude-code")

    assert cfg.headless is True and cfg.watch is None


def test_explicit_display_env_is_used_and_picks_up_the_sidecar(no_display, monkeypatch):
    _sockets, vnc = no_display
    _install_sidecar(vnc, display=":77", port=5977)
    monkeypatch.setenv("LYRA_BROWSER_DISPLAY", ":77")

    cfg = Config.from_env(client="claude-code")

    assert cfg.display == ":77" and cfg.headless is False
    assert cfg.watch == {"vnc": "127.0.0.1:5977", "display": ":77"}


def test_ipv6_listen_address_is_bracketed(no_display):
    sockets, vnc = no_display
    _install_sidecar(vnc, listen="fd7a::1")
    _x_up(sockets)
    assert Config.from_env(client="claude-code").watch["vnc"] == "[fd7a::1]:5902"


def test_malformed_sidecar_is_ignored(no_display):
    sockets, vnc = no_display
    vnc.mkdir()
    (vnc / "vnc.json").write_text("{not json")
    _x_up(sockets)
    assert read_vnc_sidecar() is None
    assert Config.from_env(client="claude-code").headless is True


# --------------------------------------------------------------------------
# Launch: the window goes to that display, and a dead one means headless
# --------------------------------------------------------------------------


def test_launch_options_hand_chrome_the_display(no_display, monkeypatch):
    sockets, vnc = no_display
    _install_sidecar(vnc)
    _x_up(sockets)
    monkeypatch.setenv("WAYLAND_DISPLAY", "")  # present but empty: must still go
    cfg = Config.from_env(client="claude-code")

    kwargs = launch_kwargs(cfg, None)

    assert kwargs["headless"] is False
    assert kwargs["env"]["DISPLAY"] == ":98"
    assert "WAYLAND_DISPLAY" not in kwargs["env"]
    assert kwargs["env"].get("LYRA_BROWSER_VNC_DIR") == str(vnc)  # the rest is kept


def test_launch_options_leave_env_alone_without_a_display(no_display):
    cfg = Config.from_env(client="claude-code")
    assert "env" not in launch_kwargs(cfg, None)


def test_session_rejudges_the_display_at_launch(no_display):
    sockets, vnc = no_display
    _install_sidecar(vnc)
    _x_up(sockets)
    cfg = Config.from_env(client="claude-code")
    session = BrowserSession(cfg)

    (sockets / "X98").unlink()
    session._settle_display()
    assert cfg.headless is True and session.display_lost == ":98" and cfg.watch is None

    _x_up(sockets)
    session._settle_display()
    assert cfg.headless is False and session.display_lost is None and cfg.watch


def test_session_never_invents_a_display(no_display):
    cfg = Config.from_env(client="claude-code")
    session = BrowserSession(cfg)
    session._settle_display()
    assert cfg.headless is True and session.display_lost is None


def test_x_server_errors_are_recognised():
    assert _is_missing_x_server(Exception("Missing X server or $DISPLAY"))
    assert _is_missing_x_server(Exception("The platform failed to initialize.  Exiting."))
    assert not _is_missing_x_server(Exception("Executable doesn't exist"))


def test_display_alive_follows_the_socket(no_display):
    sockets, _ = no_display
    assert not display_alive(":98")
    _x_up(sockets)
    assert display_alive(":98")
    assert not display_alive("otherhost:98")


# --------------------------------------------------------------------------
# What the model is told
# --------------------------------------------------------------------------


async def test_open_browser_reports_where_to_watch_and_no_password(make_ctx, tools_of, tmp_path):
    ctx = make_ctx(headless=False)
    ctx.config.display = ":98"
    ctx.config.vnc = {"display": ":98", "listen": "127.0.0.1", "port": 5902}
    secret = "S3cr3tPw"
    (tmp_path / "passwd").write_text(secret)
    tools = await tools_of(ctx)

    result = await tools["open_browser"]()

    assert result["watch"] == {"vnc": "127.0.0.1:5902", "display": ":98"}
    assert result["attended"] is True
    assert "127.0.0.1:5902" in result["note"]
    assert secret not in json.dumps(result)


async def test_open_browser_says_when_the_display_was_lost(make_ctx, tools_of):
    ctx = make_ctx(headless=True)
    ctx.session.display_lost = ":98"
    tools = await tools_of(ctx)

    result = await tools["open_browser"]()

    assert result["display_lost"] == ":98"
    assert "vnc status" in result["note"]
    assert "watch" not in result


def test_instructions_mention_vnc_and_stay_under_the_cap(no_display):
    sockets, vnc = no_display
    _install_sidecar(vnc)
    _x_up(sockets)
    text = instructions_for(Config.from_env(client="claude-code"))
    assert "VNC" in text
    assert len(text) <= 4096


# --------------------------------------------------------------------------
# The CLI: units, password, idempotence
# --------------------------------------------------------------------------


class FakeSystemctl:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        code = 1 if self.fail_on and self.fail_on in cmd else 0
        out = "Linger=yes\n" if cmd[:1] == ["loginctl"] else "active\n"
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="boom" if code else "")


def _host(tmp_path, runner=None) -> vnc_cli.Host:
    return vnc_cli.Host(
        vnc_dir=tmp_path / "vnc",
        unit_dir=tmp_path / "units",
        run=runner or FakeSystemctl(),
        which=lambda name: f"/usr/bin/{name}",
        platform="linux",
    )


def _install(host, *extra):
    return vnc_cli.main(["install", "--wait", "0", *extra], host=host)


def test_install_writes_units_sidecar_and_a_private_password(tmp_path, capsys):
    host = _host(tmp_path)

    _install(host, "--listen", "100.64.0.1")  # not answering here: exit 1, files written

    xvfb = (host.unit_dir / vnc_cli.XVFB_UNIT).read_text()
    vnc = (host.unit_dir / vnc_cli.X11VNC_UNIT).read_text()
    assert "ExecStart=/usr/bin/Xvfb :98 -screen 0 1920x1080x24 -nolisten tcp" in xvfb
    assert "-rfbport 5902" in vnc and "-listen 100.64.0.1" in vnc
    assert f"-passwdfile read:{host.password_file}" in vnc
    assert "BindsTo=lyra-xvfb.service" in vnc and "After=lyra-xvfb.service" in vnc
    assert "WantedBy=lyra-xvfb.service default.target" in vnc
    assert "$$((i+1))" in vnc, "systemd would expand a bare $i"

    password = host.password_file.read_text().strip()
    assert len(password) == 8 and password.isalnum()
    assert stat.S_IMODE(host.password_file.stat().st_mode) == 0o600
    assert password not in vnc and password not in xvfb  # never on a command line

    sidecar = json.loads(host.sidecar_file.read_text())
    assert sidecar["display"] == ":98" and sidecar["port"] == 5902
    assert sidecar["listen"] == "100.64.0.1"
    assert password not in json.dumps(sidecar)
    assert password in capsys.readouterr().out  # shown once, to the person who asked

    calls = host.run.calls
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", vnc_cli.XVFB_UNIT, vnc_cli.X11VNC_UNIT] in calls
    assert calls.index(["systemctl", "--user", "restart", vnc_cli.XVFB_UNIT]) < calls.index(
        ["systemctl", "--user", "restart", vnc_cli.X11VNC_UNIT]
    )


def test_reinstall_keeps_the_password(tmp_path, capsys):
    host = _host(tmp_path)
    _install(host)
    first = host.password_file.read_text()
    capsys.readouterr()

    _install(host, "--port", "5910")

    assert host.password_file.read_text() == first
    assert first.strip() not in capsys.readouterr().out
    assert json.loads(host.sidecar_file.read_text())["port"] == 5910


def test_reset_password_changes_it_in_place(tmp_path):
    host = _host(tmp_path)
    _install(host)
    first = host.password_file.read_text()
    assert vnc_cli.main(["reset-password"], host=host) == 0
    assert host.password_file.read_text() != first
    assert stat.S_IMODE(host.password_file.stat().st_mode) == 0o600


def test_install_refuses_without_the_binaries(tmp_path, capsys):
    host = _host(tmp_path)
    host.which = lambda name: None
    assert _install(host) == 2
    assert "Xvfb and x11vnc not found" in capsys.readouterr().err
    assert not host.password_file.exists()


def test_install_refuses_a_remote_display(tmp_path):
    assert _install(_host(tmp_path), "--display", "host:1") == 2


def test_install_refuses_off_linux(tmp_path):
    host = _host(tmp_path)
    host.platform = "darwin"
    assert _install(host) == 2


def test_install_reports_a_failing_systemctl(tmp_path, capsys):
    host = _host(tmp_path, FakeSystemctl(fail_on="enable"))
    assert _install(host) == 1
    assert "enable" in capsys.readouterr().err


def test_uninstall_moves_files_aside_and_keeps_the_password(tmp_path):
    host = _host(tmp_path)
    _install(host)
    assert vnc_cli.main(["uninstall"], host=host) == 0
    assert not (host.unit_dir / vnc_cli.XVFB_UNIT).exists()
    assert not host.sidecar_file.exists()
    assert host.password_file.exists()
    assert list((host.vnc_dir / "trash").glob("*/lyra-xvfb.service"))


def test_status_without_install(tmp_path, capsys):
    assert vnc_cli.main(["status"], host=_host(tmp_path)) == 1
    assert "not installed" in capsys.readouterr().out


def test_status_never_prints_the_password(tmp_path, capsys):
    host = _host(tmp_path)
    _install(host)
    password = host.password_file.read_text().strip()
    capsys.readouterr()
    vnc_cli.main(["status", "--json"], host=host)
    assert password not in capsys.readouterr().out


def test_main_dispatches_vnc_after_the_plugin_client_flag(monkeypatch):
    from lyra_browser import __main__ as entry

    seen = {}
    monkeypatch.setattr(vnc_cli, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    monkeypatch.setattr("sys.argv", ["lyra-browser", "--client", "claude-code", "vnc", "status"])
    with pytest.raises(SystemExit):
        entry.main()
    assert seen["argv"] == ["--client", "claude-code", "status"]


@pytest.mark.parametrize("env", [None, {"DISPLAY": ":98", "PATH": "/usr/bin"}])
async def test_native_launcher_hands_chrome_the_env(monkeypatch, tmp_path, env):
    """The native driver spawns Chrome itself: ``env`` must reach that process."""
    from lyra_browser.native import launcher
    from lyra_browser.native.errors import Error as NativeError

    seen = {}

    async def fake_exec(*args, **kwargs):
        seen.update(kwargs)
        raise OSError("stop here")

    monkeypatch.setattr(launcher, "find_executable", lambda channel: "/usr/bin/chrome")
    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", fake_exec)
    options = {"user_data_dir": str(tmp_path), "headless": False, "args": []}
    if env is not None:
        options["env"] = env

    with pytest.raises(NativeError):
        await launcher.launch(options)

    assert seen["env"] == env


def test_a_remote_display_is_left_to_chrome(no_display, monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_DISPLAY", "otherhost:0")
    cfg = Config.from_env(client="claude-code")
    session = BrowserSession(cfg)

    session._settle_display()

    assert cfg.headless is False and session.display_lost is None
    assert launch_kwargs(cfg, None)["env"]["DISPLAY"] == "otherhost:0"
