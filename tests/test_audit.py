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
