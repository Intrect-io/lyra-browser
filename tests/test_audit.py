import json

from lyra_browser.audit import AuditLog


def test_record_appends_jsonl(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("navigate", {"url": "https://example.com"})
    log.record("click", {"selector": "#go"}, status="ok")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["tool"] == "navigate"
    assert first["args"]["url"] == "https://example.com"
    assert "ts" in first


def test_record_redacts_sensitive_values(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("type_text", {"selector": "#pw", "value": "hunter2"})
    entry = json.loads((tmp_path / "audit.jsonl").read_text())
    assert entry["args"]["value"].startswith("<redacted:")
    assert "hunter2" not in (tmp_path / "audit.jsonl").read_text()


def test_mirror_stderr_repeats_each_line_verbatim(tmp_path, capsys):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, mirror_stderr=True)
    log.record("navigate", {"url": "u"})
    log.record("type_text", {"value": "hunter2"})
    err = capsys.readouterr().err
    assert err.splitlines() == path.read_text().splitlines()
    # Unprefixed JSON, so a log collector can parse the fields.
    assert json.loads(err.splitlines()[0])["tool"] == "navigate"
    assert "hunter2" not in err


def test_no_mirror_by_default(tmp_path, capsys):
    AuditLog(tmp_path / "audit.jsonl").record("navigate", {"url": "u"})
    assert capsys.readouterr().err == ""


def test_mirror_survives_an_unwritable_file(tmp_path, capsys):
    """A container whose disk fails still has stderr, and nothing raises."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed")
    log = AuditLog(blocker / "audit.jsonl", mirror_stderr=True)
    log.record("navigate", {"url": "u"})
    assert json.loads(capsys.readouterr().err)["tool"] == "navigate"
