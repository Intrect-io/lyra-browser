"""Importing the person's own browser profile: crypto, scope, swap, CLI, launch pin.

Sources here are built by hand (a Chrome-shaped Cookies database and storage dirs);
scripts/verify_profile_import_e2e.py runs the same import against real Chrome.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from lyra_browser import profile_cli
from lyra_browser.config import Config
from lyra_browser.profile_import import (
    SIDECAR_NAME,
    CookieCrypto,
    ImportError_,
    Scope,
    SourceProfile,
    _derive_key,
    _origin_host,
    build_cookies,
    import_profile,
    list_profiles,
    pinned_candidates,
    read_sidecar,
    resolve_profile,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the cookie keys tested are Linux's"
)

SCHEMA = 24
KEYRING_PASSWORD = b"a-keyring-secret"


def seal(value: bytes, host: str, prefix: bytes, password: bytes, schema: int = SCHEMA) -> bytes:
    """Encrypt a value the way Chrome on Linux does, for a test fixture."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if schema >= 24:
        value = hashlib.sha256(host.encode()).digest() + value
    n = 16 - len(value) % 16
    padded = value + bytes([n]) * n
    enc = Cipher(algorithms.AES(_derive_key(password)), modes.CBC(b" " * 16)).encryptor()
    return prefix + enc.update(padded) + enc.finalize()


def make_cookie_db(path: Path, rows: list[tuple[str, str, bytes]], schema: int = SCHEMA) -> None:
    """A Cookies database with Chrome's key columns. ``rows``: (host, name, encrypted)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("create table meta(key longvarchar not null unique primary key, value longvarchar)")
    db.execute(
        "insert into meta values ('version', ?), ('last_compatible_version', ?)", (schema,) * 2
    )
    db.execute(
        "create table cookies(creation_utc integer, host_key text, top_frame_site_key text,"
        " name text, value text, encrypted_value blob, path text, expires_utc integer,"
        " unique(host_key, top_frame_site_key, name, path))"
    )
    db.executemany(
        "insert into cookies values (1, ?, '', ?, '', ?, '/', 0)",
        [(host, name, enc) for host, name, enc in rows],
    )
    db.commit()
    db.close()


def read_cookies(path: Path, password: bytes = b"peanuts") -> dict[tuple[str, str], bytes | None]:
    crypto = CookieCrypto()
    db = sqlite3.connect(path)
    try:
        rows = db.execute("select host_key, name, encrypted_value from cookies").fetchall()
    finally:
        db.close()
    out = {}
    for host, name, enc in rows:
        enc = bytes(enc)
        assert enc[:3] == b"v10", "lyra's Chrome reads v10 only"
        out[(host, name)] = crypto.decrypt(enc, host, SCHEMA)
    return out


def make_source(root: Path, rows: list[tuple[str, str, bytes]], directory: str = "Default"):
    profile = root / directory
    make_cookie_db(profile / "Cookies", rows)
    leveldb = profile / "Local Storage" / "leveldb"
    leveldb.mkdir(parents=True)
    (leveldb / "000003.log").write_bytes(b"local-storage")
    (leveldb / "LOCK").write_bytes(b"")
    for name in ("https_example.com_0.indexeddb.leveldb", "https_other.org_0.indexeddb.leveldb"):
        (profile / "IndexedDB" / name).mkdir(parents=True)
        (profile / "IndexedDB" / name / "CURRENT").write_text(name)
    (root / "Local State").write_text(
        json.dumps({"profile": {"info_cache": {directory: {"name": "Work", "user_name": "a@b"}}}})
    )
    (root / "Last Version").write_text("154.0.8037.57")
    return SourceProfile("chrome", root, directory, name="Work")


def make_config(tmp_path: Path, **kw) -> Config:
    return Config(client="generic", data_dir=tmp_path / "lyra", headless=True, **kw)


def v11_crypto() -> CookieCrypto:
    return CookieCrypto([KEYRING_PASSWORD])


# --------------------------------------------------------------------------- crypto


@pytest.mark.parametrize("prefix,password", [(b"v10", b"peanuts"), (b"v11", KEYRING_PASSWORD)])
def test_decrypt_opens_both_linux_seals(prefix, password):
    sealed = seal(b"session-token", "example.com", prefix, password)
    assert v11_crypto().decrypt(sealed, "example.com", SCHEMA) == b"session-token"


def test_a_v11_cookie_without_its_keyring_password_stays_shut():
    sealed = seal(b"session-token", "example.com", b"v11", KEYRING_PASSWORD)
    assert CookieCrypto().decrypt(sealed, "example.com", SCHEMA) is None


def test_the_domain_hash_tells_the_right_host_from_a_wrong_one():
    sealed = seal(b"x", "example.com", b"v10", b"peanuts")
    assert CookieCrypto().decrypt(sealed, "evil.example", SCHEMA) is None


def test_older_schemas_have_no_domain_hash():
    sealed = seal(b"value", "example.com", b"v10", b"peanuts", schema=23)
    assert CookieCrypto().decrypt(sealed, "example.com", 23) == b"value"


def test_reencryption_round_trips_as_v10():
    crypto = CookieCrypto()
    sealed = crypto.encrypt_v10(b"value", ".example.com", SCHEMA)
    assert sealed[:3] == b"v10"
    assert crypto.decrypt(sealed, ".example.com", SCHEMA) == b"value"


def test_app_bound_and_unknown_seals_are_not_guessed_at():
    assert CookieCrypto([b"x"]).decrypt(b"v20" + b"\0" * 48, "example.com", SCHEMA) is None


# --------------------------------------------------------------------------- scope


def test_a_site_covers_its_subdomains_and_domain_cookies():
    scope = Scope(sites=("example.com",))
    assert scope.wants_host("example.com")
    assert scope.wants_host(".example.com")
    assert scope.wants_host("app.example.com")
    assert not scope.wants_host("notexample.com")
    assert not scope.wants_host("example.com.evil.io")


def test_no_sites_means_every_site():
    assert Scope().wants_host("anything.test")
    assert not Scope().filtered


def test_denied_sites_beat_the_import():
    scope = Scope(denied=("bank.example",))
    assert not scope.wants_host("bank.example")
    assert scope.wants_host("other.example")


def test_a_domain_cookie_that_reaches_a_denied_host_is_dropped():
    scope = Scope(denied=("pay.example.com",))
    assert not scope.wants_host(".example.com")  # would be sent to pay.example.com
    assert scope.wants_host("www.example.com")  # host-only: never sent there


def test_a_denied_wildcard_covers_hosts_below_it():
    scope = Scope(denied=("*.example.com",))
    assert not scope.wants_host("a.example.com")
    assert scope.wants_host("example.com")
    assert not scope.wants_host(".example.com")


@pytest.mark.parametrize(
    "text,host",
    [
        ("https_example.com_0.indexeddb.leveldb", "example.com"),
        ("http_127.0.0.1_8080.indexeddb.leveldb", "127.0.0.1"),
        ("https://a.example.com/", "a.example.com"),
        ("https://a.example.com/^0https://top.test", "a.example.com"),
        ("chrome-extension://abc/", ""),
        ("chrome-extension_abc_0.indexeddb.leveldb", ""),
    ],
)
def test_origin_host_reads_every_storage_naming(text, host):
    assert _origin_host(text) == host


# --------------------------------------------------------------------------- cookies


def test_build_cookies_reseals_in_scope_rows_and_counts_the_rest(tmp_path):
    src = tmp_path / "src.db"
    make_cookie_db(
        src,
        [
            ("example.com", "a", seal(b"1", "example.com", b"v11", KEYRING_PASSWORD)),
            ("other.org", "b", seal(b"2", "other.org", b"v10", b"peanuts")),
            ("example.com", "c", seal(b"3", "example.com", b"v11", b"wrong")),
        ],
    )
    out = tmp_path / "out.db"
    report = build_cookies(src, None, out, v11_crypto(), Scope(sites=("example.com",)))

    assert (report.in_source, report.imported, report.out_of_scope, report.undecryptable) == (
        3,
        1,
        1,
        1,
    )
    assert report.by_prefix == {"v11": 2, "v10": 1}
    assert read_cookies(out) == {("example.com", "a"): b"1"}


def test_build_cookies_keeps_lyra_cookies_out_of_scope_and_replaces_the_rest(tmp_path):
    src, current, out = tmp_path / "src.db", tmp_path / "cur.db", tmp_path / "out.db"
    make_cookie_db(src, [("example.com", "a", seal(b"new", "example.com", b"v10", b"peanuts"))])
    make_cookie_db(
        current,
        [
            ("example.com", "a", seal(b"old", "example.com", b"v10", b"peanuts")),
            ("example.com", "stale", seal(b"s", "example.com", b"v10", b"peanuts")),
            ("mine.test", "m", seal(b"keep", "mine.test", b"v10", b"peanuts")),
        ],
    )
    report = build_cookies(src, current, out, CookieCrypto(), Scope(sites=("example.com",)))

    assert (report.replaced, report.kept, report.imported) == (2, 1, 1)
    assert read_cookies(out) == {("example.com", "a"): b"new", ("mine.test", "m"): b"keep"}


def test_build_cookies_refuses_a_schema_from_another_version(tmp_path):
    src, current = tmp_path / "src.db", tmp_path / "cur.db"
    make_cookie_db(src, [], schema=24)
    make_cookie_db(current, [], schema=23)
    with pytest.raises(ImportError_, match="schema 23"):
        build_cookies(src, current, tmp_path / "out.db", CookieCrypto(), Scope())


# --------------------------------------------------------------------------- import


def test_import_moves_cookies_and_storage_and_records_where_from(tmp_path):
    source = make_source(
        tmp_path / "chrome",
        [("example.com", "sid", seal(b"tok", "example.com", b"v11", KEYRING_PASSWORD))],
    )
    cfg = make_config(tmp_path)
    result = import_profile(cfg, source, crypto=v11_crypto())

    target = cfg.profile_dir / "Default"
    assert read_cookies(target / "Cookies") == {("example.com", "sid"): b"tok"}
    assert (target / "Local Storage" / "leveldb" / "000003.log").read_bytes() == b"local-storage"
    assert not (target / "Local Storage" / "leveldb" / "LOCK").exists()
    assert sorted(p.name for p in (target / "IndexedDB").iterdir()) == [
        "https_example.com_0.indexeddb.leveldb",
        "https_other.org_0.indexeddb.leveldb",
    ]
    sidecar = read_sidecar(cfg)
    assert sidecar["profile"] == "Default" and sidecar["source_version"] == "154.0.8037.57"
    assert sidecar == result.sidecar
    assert not (cfg.data_dir / "profile.import-staging").exists()
    assert not (cfg.data_dir / "profile.import-retired").exists()


def test_a_site_list_filters_indexeddb_and_leaves_local_storage_out(tmp_path):
    source = make_source(tmp_path / "chrome", [])
    cfg = make_config(tmp_path)
    result = import_profile(cfg, source, sites=["example.com"], crypto=CookieCrypto())

    target = cfg.profile_dir / "Default"
    assert [p.name for p in (target / "IndexedDB").iterdir()] == [
        "https_example.com_0.indexeddb.leveldb"
    ]
    assert not (target / "Local Storage").exists()
    assert "cannot be filtered" in result.components["Local Storage"]


def test_denied_origins_keep_their_cookies_and_stores_out(tmp_path):
    source = make_source(
        tmp_path / "chrome",
        [
            ("other.org", "x", seal(b"1", "other.org", b"v10", b"peanuts")),
            ("example.com", "y", seal(b"2", "example.com", b"v10", b"peanuts")),
        ],
    )
    cfg = make_config(tmp_path, denied_origins=("other.org",))
    import_profile(cfg, source, crypto=CookieCrypto())

    target = cfg.profile_dir / "Default"
    assert read_cookies(target / "Cookies") == {("example.com", "y"): b"2"}
    assert [p.name for p in (target / "IndexedDB").iterdir()] == [
        "https_example.com_0.indexeddb.leveldb"
    ]
    assert not (target / "Local Storage").exists()


def test_a_dry_run_changes_nothing(tmp_path):
    source = make_source(
        tmp_path / "chrome", [("a.test", "n", seal(b"v", "a.test", b"v10", b"peanuts"))]
    )
    cfg = make_config(tmp_path)
    result = import_profile(cfg, source, crypto=CookieCrypto(), dry_run=True)

    assert result.cookies.imported == 1
    assert not (cfg.profile_dir / "Default").exists()
    assert read_sidecar(cfg) is None


def test_import_refuses_while_a_lyra_browser_holds_the_profile(tmp_path):
    source = make_source(tmp_path / "chrome", [])
    cfg = make_config(tmp_path)
    cfg.data_dir.mkdir(parents=True)
    holder = os.open(cfg.data_dir / "profile.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(ImportError_, match="running"):
            import_profile(cfg, source, crypto=CookieCrypto())
    finally:
        os.close(holder)


def test_the_new_cookie_file_never_inherits_an_old_journal(tmp_path):
    source = make_source(
        tmp_path / "chrome", [("a.test", "n", seal(b"v", "a.test", b"v10", b"peanuts"))]
    )
    cfg = make_config(tmp_path)
    target = cfg.profile_dir / "Default"
    make_cookie_db(target / "Cookies", [])
    (target / "Cookies-journal").write_bytes(b"hot journal of the old file")

    import_profile(cfg, source, crypto=CookieCrypto())

    assert not (target / "Cookies-journal").exists()
    assert read_cookies(target / "Cookies") == {("a.test", "n"): b"v"}


def test_v11_cookies_with_a_locked_keyring_refuse_rather_than_half_import(tmp_path, monkeypatch):
    from lyra_browser import profile_import

    source = make_source(
        tmp_path / "chrome",
        [("a.test", "n", seal(b"v", "a.test", b"v11", KEYRING_PASSWORD))],
    )
    asked = []

    def locked(app, *, unlock=False):
        asked.append((app, unlock))
        return None, "locked"

    monkeypatch.setattr(profile_import, "read_keyring_password", locked)
    cfg = make_config(tmp_path)
    with pytest.raises(ImportError_, match="--unlock-keyring"):
        import_profile(cfg, source, unlock_keyring=True)
    assert asked == [("chrome", True)]
    assert not (cfg.profile_dir / "Default").exists()


def test_the_keyring_is_only_asked_when_a_v11_cookie_needs_it(tmp_path, monkeypatch):
    from lyra_browser import profile_import

    source = make_source(
        tmp_path / "chrome", [("a.test", "n", seal(b"v", "a.test", b"v10", b"peanuts"))]
    )
    monkeypatch.setattr(
        profile_import, "read_keyring_password", lambda *a, **k: pytest.fail("asked")
    )
    result = import_profile(make_config(tmp_path), source)
    assert result.keyring == "not-needed"


# --------------------------------------------------------------------------- discovery


def test_profiles_are_found_by_directory_or_shown_name(tmp_path):
    root = tmp_path / "chrome"
    make_source(root, [])
    (root / "Profile 2").mkdir()
    state = json.loads((root / "Local State").read_text())
    state["profile"]["info_cache"]["Profile 2"] = {"name": "Home"}
    (root / "Local State").write_text(json.dumps(state))

    assert [p.directory for p in list_profiles("chrome", root)] == ["Default", "Profile 2"]
    assert resolve_profile("chrome", root, "home").directory == "Profile 2"
    assert resolve_profile("chrome", root, "Default").name == "Work"
    with pytest.raises(ImportError_, match="found: Default"):
        resolve_profile("chrome", root, "nope")


# --------------------------------------------------------------------------- launch + tools


def test_an_imported_profile_pins_the_browser_that_wrote_it(tmp_path):
    cfg = make_config(tmp_path)
    assert pinned_candidates(cfg) == cfg.browser_candidates()
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / SIDECAR_NAME).write_text(json.dumps({"browser": "chrome"}))
    assert pinned_candidates(cfg) == ["chrome"]


def test_an_operator_channel_still_wins_over_the_pin(tmp_path):
    cfg = make_config(tmp_path, channel="msedge")
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / SIDECAR_NAME).write_text(json.dumps({"browser": "chrome"}))
    assert pinned_candidates(cfg) == cfg.browser_candidates()
    assert pinned_candidates(cfg)[0] == "msedge"


async def test_open_browser_says_the_profile_acts_as_the_user(make_ctx, tools_of):
    ctx = make_ctx()
    ctx.session.profile_mode = "shared"
    sidecar = {"browser": "chrome", "profile": "Default", "name": "Work", "sites": []}
    (ctx.config.data_dir / SIDECAR_NAME).write_text(json.dumps(sidecar))
    tools = await tools_of(ctx)

    result = await tools["open_browser"]()

    assert result["imported_from"]["name"] == "Work"
    assert "act as them" in result["note"]


async def test_a_private_instance_does_not_claim_the_imported_logins(make_ctx, tools_of):
    ctx = make_ctx()
    ctx.session.profile_mode = "instance"
    (ctx.config.data_dir / SIDECAR_NAME).write_text(json.dumps({"browser": "chrome"}))
    tools = await tools_of(ctx)

    result = await tools["open_browser"]()

    assert "imported_from" not in result


# --------------------------------------------------------------------------- cli


def test_cli_import_and_status(tmp_path, capsys):
    root = tmp_path / "chrome"
    make_source(root, [("a.test", "n", seal(b"v", "a.test", b"v10", b"peanuts"))])
    data = tmp_path / "lyra"
    argv = ["--user-data-dir", str(root), "--profile", "Work", "--data-dir", str(data)]

    assert profile_cli.main(["import", *argv]) == 0
    out = capsys.readouterr().out
    assert "imported chrome profile 'Default' (Work)" in out
    assert "1 imported of 1" in out

    assert profile_cli.main(["status", "--data-dir", str(data)]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "Work"


def test_cli_reports_a_refusal_without_a_traceback(tmp_path, capsys):
    root = tmp_path / "chrome"
    make_source(root, [])
    argv = ["import", "--user-data-dir", str(root), "--profile", "nope"]
    assert profile_cli.main([*argv, "--data-dir", str(tmp_path / "lyra")]) == 1
    assert "no profile 'nope'" in capsys.readouterr().err


def test_session_cookies_are_counted_and_left_out(tmp_path):
    src = tmp_path / "src.db"
    make_cookie_db(src, [("a.test", "n", seal(b"v", "a.test", b"v10", b"peanuts"))])
    db = sqlite3.connect(src)
    db.execute("alter table cookies add column is_persistent integer default 1")
    db.execute(
        "insert into cookies (creation_utc, host_key, top_frame_site_key, name, value,"
        " encrypted_value, path, expires_utc, is_persistent)"
        " values (1, 'a.test', '', 's', '', ?, '/', 0, 0)",
        (seal(b"x", "a.test", b"v10", b"peanuts"),),
    )
    db.commit()
    db.close()
    report = build_cookies(src, None, tmp_path / "out.db", CookieCrypto(), Scope())
    assert (report.imported, report.session) == (1, 1)
    assert read_cookies(tmp_path / "out.db") == {("a.test", "n"): b"v"}


def test_a_failed_swap_puts_the_old_profile_back(tmp_path, monkeypatch):
    from lyra_browser import profile_import

    source = make_source(
        tmp_path / "chrome", [("a.test", "n", seal(b"new", "a.test", b"v10", b"peanuts"))]
    )
    cfg = make_config(tmp_path)
    target = cfg.profile_dir / "Default"
    make_cookie_db(
        target / "Cookies", [("a.test", "n", seal(b"old", "a.test", b"v10", b"peanuts"))]
    )
    (target / "Cookies-journal").write_bytes(b"journal")
    (target / "IndexedDB" / "mine").mkdir(parents=True)
    old_cookies = (target / "Cookies").read_bytes()

    real_replace = os.replace

    def failing(src, dst):
        # Placing the new IndexedDB fails, and so does putting the old one back:
        # the other components must still be restored, and nothing deleted.
        if Path(dst).name == "IndexedDB" and Path(dst).parent == target:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(profile_import.os, "replace", failing)
    with pytest.raises(OSError, match="disk full"):
        import_profile(cfg, source, crypto=CookieCrypto())

    assert (target / "Cookies").read_bytes() == old_cookies
    assert (target / "Cookies-journal").read_bytes() == b"journal"
    assert (cfg.data_dir / "profile.import-retired" / "IndexedDB" / "mine").is_dir()
    assert (target / "Local Storage").exists() is False
    assert read_sidecar(cfg) is None

    monkeypatch.setattr(profile_import.os, "replace", real_replace)
    import_profile(cfg, source, crypto=CookieCrypto(), dry_run=True)
    assert (target / "IndexedDB" / "mine").is_dir()  # the next run restores it first


def test_a_swap_a_killed_import_left_is_restored_first(tmp_path):
    source = make_source(tmp_path / "chrome", [])
    cfg = make_config(tmp_path)
    retired = cfg.data_dir / "profile.import-retired"
    (retired / "Local Storage").mkdir(parents=True)
    (retired / "Local Storage" / "x").write_text("lyra's own")
    (cfg.profile_dir / "Default").mkdir(parents=True)

    import_profile(cfg, source, crypto=CookieCrypto(), dry_run=True)

    restored = cfg.profile_dir / "Default" / "Local Storage" / "x"
    assert restored.read_text() == "lyra's own"
    assert not retired.exists()


def test_the_plugin_launchers_leading_client_flag_reaches_the_profile_command(
    tmp_path, monkeypatch, capsys
):
    from lyra_browser import __main__ as entry

    seen = {}
    monkeypatch.setattr(profile_cli, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    monkeypatch.setattr(sys, "argv", ["lyra-browser", "--client", "claude-code", "profile", "list"])
    with pytest.raises(SystemExit):
        entry.main()
    assert seen["argv"] == ["list", "--client", "claude-code"]
    assert profile_cli.build_parser().parse_args(seen["argv"]).client == "claude-code"


def test_a_network_cookies_layout_is_read_and_written_in_place(tmp_path):
    source = make_source(tmp_path / "chrome", [])
    (source.path / "Cookies").unlink()
    make_cookie_db(
        source.path / "Network" / "Cookies",
        [("a.test", "n", seal(b"v", "a.test", b"v10", b"peanuts"))],
    )
    cfg = make_config(tmp_path)
    network = cfg.profile_dir / "Default" / "Network"
    make_cookie_db(
        network / "Cookies", [("old.test", "o", seal(b"o", "old.test", b"v10", b"peanuts"))]
    )
    (network / "Trust Tokens").write_bytes(b"lyra's")

    result = import_profile(cfg, source, crypto=CookieCrypto())

    assert result.cookies.imported == 1
    assert read_cookies(network / "Cookies") == {("a.test", "n"): b"v"}
    assert (network / "Trust Tokens").read_bytes() == b"lyra's"
    assert not (cfg.profile_dir / "Default" / "Cookies").exists()


def test_an_interrupted_network_swap_restores_the_old_cookie_file(tmp_path):
    cfg = make_config(tmp_path)
    network = cfg.profile_dir / "Default" / "Network"
    network.mkdir(parents=True)
    (network / "Trust Tokens").write_bytes(b"lyra's")
    retired = cfg.data_dir / "profile.import-retired" / "Network"
    retired.mkdir(parents=True)
    make_cookie_db(
        retired / "Cookies", [("old.test", "o", seal(b"o", "old.test", b"v10", b"peanuts"))]
    )

    import_profile(cfg, make_source(tmp_path / "chrome", []), crypto=CookieCrypto(), dry_run=True)

    assert read_cookies(network / "Cookies") == {("old.test", "o"): b"o"}
    assert (network / "Trust Tokens").read_bytes() == b"lyra's"


def test_off_linux_no_cookie_is_guessed_open(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    sealed = seal(b"x", "a.test", b"v10", b"peanuts")
    assert CookieCrypto().decrypt(sealed, "a.test", SCHEMA) is None
