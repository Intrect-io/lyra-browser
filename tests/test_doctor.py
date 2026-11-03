"""``lyra-browser doctor``: the report a launch would agree with, and nothing launched."""

from __future__ import annotations

import fcntl
import json
import os
import sys

import pytest

from lyra_browser import __main__ as cli
from lyra_browser import doctor
from lyra_browser.config import Config
from lyra_browser.native.errors import Error as NativeError
from lyra_browser.profile_import import SIDECAR_NAME


def make_config(tmp_path, **kw) -> Config:
    kw.setdefault("headless", True)
    return Config(client="generic", data_dir=tmp_path / "lyra", **kw)


def finder(found: dict[str | None, str]):
    """A ``find_executable`` that knows only ``found``, worded like the real one."""

    def find(channel):
        if channel in found:
            return found[channel]
        raise NativeError(f'Chromium distribution "{channel or "bundled"}" is not found')

    return find


def installed(*names: str):
    return lambda name: name in names


def run(config: Config, *, found=None, packages=(), display=True, held=False) -> dict:
    return doctor.diagnose(
        config,
        find=finder(found if found is not None else {"chrome": "/opt/chrome"}),
        installed=installed(*packages),
        has_display=lambda: display,
        lock_held=lambda _path: held,
    )


def errors(report: dict) -> list[str]:
    return [p["what"] for p in report["problems"] if p["level"] == "error"]


def test_a_machine_with_chrome_is_ok_on_native(tmp_path):
    report = run(make_config(tmp_path))
    assert report["ok"] is True
    assert report["driver"]["resolved"] == "native"
    assert report["browser"]["would_launch"] == {"channel": "chrome", "path": "/opt/chrome"}
    assert report["guard_backend"] == "cdp", "native always runs the cdp guard"
    assert report["problems"] == []
    json.dumps(report)  # the --json output must serialise


def test_no_browser_and_no_playwright_is_an_error_that_names_the_installer(tmp_path):
    report = run(make_config(tmp_path), found={})
    assert report["ok"] is False
    assert report["driver"]["resolved"] is None
    assert any("install-browser" in p["fix"] for p in report["problems"])


def test_auto_passes_over_native_to_a_playwright_that_brings_its_chromium(tmp_path):
    report = run(make_config(tmp_path), found={None: "/cache/chromium"}, packages=("playwright",))
    # native can drive the bundled Chromium too, so it is still the choice — as in a launch.
    assert report["driver"]["resolved"] == "native"
    report = run(make_config(tmp_path), found={}, packages=("playwright",))
    assert report["driver"]["resolved"] == "playwright"
    assert "native" in report["driver"]["why"]
    assert report["ok"] is False, "no Chromium downloaded yet: the launch would still fail"


def test_a_pinned_driver_that_is_not_installed_says_how_to_install_it(tmp_path):
    report = run(make_config(tmp_path, driver="patchright"))
    assert report["ok"] is False
    assert any("lyra-browser[patchright]" in p["fix"] for p in report["problems"])


def test_the_imported_profile_pin_is_what_the_report_looks_for(tmp_path):
    cfg = make_config(tmp_path)
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / SIDECAR_NAME).write_text(
        json.dumps({"browser": "msedge", "name": "Work", "cookies": {"imported": 12}})
    )
    report = run(cfg, found={"chrome": "/opt/chrome"})
    assert report["browser"]["candidates"] == ["msedge"]
    assert report["browser"]["pinned_by_import"] == "msedge"
    assert report["imported_profile"]["profile"] == "Work"
    assert report["imported_profile"]["cookies_imported"] == 12
    assert report["ok"] is False, "chrome is installed, but the pin forbids falling back to it"
    assert any("LYRA_BROWSER_CHANNEL" in p["fix"] for p in report["problems"])


def test_an_operator_channel_beats_the_pin_here_as_at_launch(tmp_path):
    cfg = make_config(tmp_path, channel="chrome")
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / SIDECAR_NAME).write_text(json.dumps({"browser": "msedge"}))
    report = run(cfg)
    assert report["browser"]["pinned_by_import"] is None
    assert report["ok"] is True


def test_a_held_profile_lock_is_a_warning_not_a_failure(tmp_path):
    report = run(make_config(tmp_path), held=True)
    assert report["ok"] is True
    assert [p["level"] for p in report["problems"]] == ["warning"]


def test_the_real_lock_probe_sees_a_holder_and_takes_nothing(tmp_path):
    lock = tmp_path / "profile.lock"
    assert doctor._lock_held_by_other(lock) is False
    assert not lock.exists(), "probing must not create the lock file"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert doctor._lock_held_by_other(lock) is True
        fcntl.flock(fd, fcntl.LOCK_UN)
        assert doctor._lock_held_by_other(lock) is False
        # ...and gave the claim back: a server can still take it.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_a_window_without_a_display_is_an_error(tmp_path):
    report = run(make_config(tmp_path, headless=False), display=False)
    assert report["ok"] is False
    assert any("LYRA_BROWSER_HEADLESS" in p["fix"] for p in report["problems"])
    assert run(make_config(tmp_path, headless=True), display=False)["ok"] is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_data_dir_that_cannot_be_written_is_an_error(tmp_path):
    locked = tmp_path / "ro"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        report = run(Config(client="generic", data_dir=locked / "lyra", headless=True))
    finally:
        locked.chmod(0o700)
    assert report["ok"] is False
    assert any("not writable" in w for w in errors(report))


def test_doctor_creates_nothing(tmp_path):
    cfg = make_config(tmp_path)
    report = run(cfg)
    assert report["data_dir"]["writable"] is True
    assert not cfg.data_dir.exists()
    assert list(tmp_path.iterdir()) == [], "the write probe is removed"


def test_the_text_report_names_each_fix(tmp_path):
    text = doctor.render(run(make_config(tmp_path), found={}))
    assert "fix:" in text and "install-browser" in text
    assert text.endswith("NOT ok: a launch would fail")


def test_the_cli_answers_json_and_its_exit_code_is_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_find_executable", finder({"chrome": "/opt/chrome"}))
    monkeypatch.setattr(doctor, "_lock_held_by_other", lambda _p: False)
    monkeypatch.setenv("LYRA_BROWSER_HEADLESS", "1")
    data = tmp_path / "d"
    # The plugin launcher puts --client first, as for `profile`.
    argv = ["lyra-browser", "--client", "generic", "doctor", "--json", "--data-dir", str(data)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as done:
        cli.main()
    report = json.loads(capsys.readouterr().out)
    assert done.value.code == 0 and report["ok"] is True
    assert report["data_dir"]["path"] == str(data)
    assert report["client"] == "generic"

    monkeypatch.setattr(doctor, "_find_executable", finder({}))
    monkeypatch.setattr(doctor, "_has_display", lambda: True)
    monkeypatch.setattr("lyra_browser.session.installed", installed())
    assert doctor.main(["--data-dir", str(data), "--headless"]) == 1


def test_a_live_vnc_display_counts_as_a_display(tmp_path, monkeypatch):
    from lyra_browser import config as config_mod

    sockets = tmp_path / "x11"
    sockets.mkdir()
    (sockets / "X98").touch()
    monkeypatch.setattr(config_mod, "X11_SOCKET_DIR", sockets)
    cfg = make_config(
        tmp_path,
        headless=False,
        display=":98",
        vnc={"display": ":98", "listen": "127.0.0.1", "port": 5902},
    )

    report = run(cfg, display=False)

    assert report["ok"] is True, report["problems"]
    assert report["display"]["watch"] == {"vnc": "127.0.0.1:5902", "display": ":98"}
    assert "VNC 127.0.0.1:5902 on :98" in doctor.render(report)


def test_a_configured_display_that_is_down_is_a_warning(tmp_path, monkeypatch):
    from lyra_browser import config as config_mod

    monkeypatch.setattr(config_mod, "X11_SOCKET_DIR", tmp_path / "empty")
    report = run(make_config(tmp_path, headless=False, display=":98"), display=False)
    assert report["ok"] is False
    assert any("not up" in p["what"] for p in report["problems"])
