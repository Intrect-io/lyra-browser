"""Bring the logins of the person's own Chrome profile into lyra's profile.

A human runs ``lyra-browser profile import``; nothing on the MCP side can. The
model never decides to become the user on every site: the operator does, once,
on the command line, and re-running the command is how the two stay in sync.

What moves, per component (and nothing else):

- ``Cookies`` (SQLite): row by row. Every cookie is decrypted with the source's
  key and encrypted again with the key lyra's Chrome reads. Playwright launches
  Chrome with ``--password-store=basic``, so lyra's cookies are ``v10`` (a fixed,
  public key), while a Chrome that found a desktop keyring writes ``v11`` under a
  secret only that keyring knows. A ``v11`` cookie copied as-is never decrypts in
  lyra, so it is opened here (Secret Service over D-Bus) and written back as
  ``v10``. A cookie that cannot be opened is counted and left out, never copied
  as-is. Session cookies are left out too: Chrome deletes them when it starts.
- ``Local Storage`` (one LevelDB for all sites): whole, or not at all. It cannot
  be filtered by site without a LevelDB writer, so a site list or a deny list
  leaves it out rather than carrying sites nobody asked for.
- ``IndexedDB`` (one directory per origin) and ``WebStorage`` (storage buckets,
  indexed by its ``QuotaManager`` database): filtered by site.

Not moved, on purpose: saved passwords (``Login Data``), extensions, settings
(``Preferences`` holds absolute paths and account tokens), history, and Session
Storage (it belongs to tabs that are gone).

Sync means *replace*: the cookies in scope (every cookie, or the listed sites')
and the other components are replaced by the source's, so whatever lyra's own
sessions stored for them is overwritten. Cookies out of scope are kept.

The source is read-only and may be running. SQLite files are read with the
backup API, which takes a consistent snapshot under SQLite's own locking;
LevelDB directories are copied file by file, skipping ``LOCK``, and copied again
when the database changed underneath (a few writes of staleness is the price of
not stopping the person's browser).

Linux only for decryption. macOS (Keychain) and Windows (DPAPI, app-bound
``v20``) cookies are reported as undecryptable instead of guessed at.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, parse_trusted_origins

# The file beside the profile that says where its imported part came from.
SIDECAR_NAME = "profile-import.json"
# Where an import builds the new components before swapping them in.
_STAGING_NAME = "profile.import-staging"
# Where the replaced components wait until the swap is complete.
_RETIRED_NAME = "profile.import-retired"
# lyra's Chrome opens a single profile, "Default", inside its user data dir.
TARGET_PROFILE = "Default"

# Chrome's Linux cookie crypto (components/os_crypt/sync/os_crypt_linux.cc):
# AES-128-CBC, IV of 16 spaces, key = PBKDF2-HMAC-SHA1(password, "saltysalt", 1).
# "peanuts" is the v10 password, the one a basic password store uses.
_SALT = b"saltysalt"
_IV = b" " * 16
_V10_PASSWORD = b"peanuts"
# From cookie schema 24 (Chrome 130) the plaintext starts with SHA-256(host_key),
# which binds a value to its domain. It also tells a right key from a wrong one.
_DOMAIN_HASH_SCHEMA = 24

# Secret Service ``application`` attribute per browser, as each one stores its
# "<Name> Safe Storage" password.
KEYRING_APPLICATIONS = {
    "chrome": "chrome",
    "chrome-beta": "chrome",
    "chromium": "chromium",
    "msedge": "microsoft-edge",
    "brave": "brave",
}


def _linux_roots() -> dict[str, Path]:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return {
        "chrome": config / "google-chrome",
        "chrome-beta": config / "google-chrome-beta",
        "chromium": config / "chromium",
        "msedge": config / "microsoft-edge",
        "brave": config / "BraveSoftware" / "Brave-Browser",
    }


def _mac_roots() -> dict[str, Path]:
    base = Path.home() / "Library" / "Application Support"
    return {
        "chrome": base / "Google" / "Chrome",
        "chrome-beta": base / "Google" / "Chrome Beta",
        "chromium": base / "Chromium",
        "msedge": base / "Microsoft Edge",
        "brave": base / "BraveSoftware" / "Brave-Browser",
    }


def _windows_roots() -> dict[str, Path]:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return {
        "chrome": base / "Google" / "Chrome" / "User Data",
        "chrome-beta": base / "Google" / "Chrome Beta" / "User Data",
        "chromium": base / "Chromium" / "User Data",
        "msedge": base / "Microsoft" / "Edge" / "User Data",
        "brave": base / "BraveSoftware" / "Brave-Browser" / "User Data",
    }


def browser_roots() -> dict[str, Path]:
    """Where each browser keeps its user data dir on this platform."""
    if sys.platform == "darwin":
        return _mac_roots()
    if sys.platform == "win32":
        return _windows_roots()
    return _linux_roots()


class ImportError_(Exception):  # noqa: N801 - ImportError is a builtin
    """The import cannot go ahead; the message says why and what to do."""


# --------------------------------------------------------------------------- discovery


@dataclass(slots=True)
class SourceProfile:
    """One profile of a Chromium-family browser on this machine."""

    browser: str
    user_data_dir: Path
    directory: str  # "Default", "Profile 2", ...
    name: str = ""
    account: str = ""

    @property
    def path(self) -> Path:
        return self.user_data_dir / self.directory


def list_profiles(browser: str, user_data_dir: Path) -> list[SourceProfile]:
    """The profiles ``Local State`` names, in its order; empty if there is none."""
    try:
        state = json.loads((user_data_dir / "Local State").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    cache = (state.get("profile") or {}).get("info_cache") or {}
    found = [
        SourceProfile(
            browser,
            user_data_dir,
            directory,
            name=str(info.get("name") or ""),
            account=str(info.get("user_name") or ""),
        )
        for directory, info in cache.items()
        if isinstance(info, dict) and (user_data_dir / directory).is_dir()
    ]
    if not found and (user_data_dir / "Default").is_dir():
        found.append(SourceProfile(browser, user_data_dir, "Default"))
    return found


def resolve_profile(browser: str, user_data_dir: Path, wanted: str) -> SourceProfile:
    """The profile ``wanted`` names: its directory ("Profile 2") or its shown name."""
    profiles = list_profiles(browser, user_data_dir)
    for profile in profiles:
        if wanted == profile.directory:
            return profile
    by_name = [p for p in profiles if wanted.casefold() == p.name.casefold()]
    if len(by_name) == 1:
        return by_name[0]
    known = ", ".join(f"{p.directory} ({p.name})" if p.name else p.directory for p in profiles)
    if len(by_name) > 1:
        raise ImportError_(
            f"{len(by_name)} profiles are called {wanted!r}; name its directory: {known}"
        )
    raise ImportError_(f"no profile {wanted!r} in {user_data_dir}; found: {known or 'none'}")


def source_version(user_data_dir: Path) -> str:
    """The browser version that last wrote this user data dir ("" if unknown)."""
    try:
        return (user_data_dir / "Last Version").read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return ""


# --------------------------------------------------------------------------- crypto


def _derive_key(password: bytes, iterations: int = 1) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, _SALT, iterations, dklen=16)


def _aes_cbc(key: bytes, data: bytes, *, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key), modes.CBC(_IV))
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(data) + op.finalize()


def _pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _unpad(data: bytes) -> bytes | None:
    if not data or len(data) % 16:
        return None
    n = data[-1]
    if not 1 <= n <= 16 or data[-n:] != bytes([n]) * n:
        return None
    return data[:-n]


def read_keyring_password(application: str, *, unlock: bool = False) -> tuple[bytes | None, str]:
    """The "Safe Storage" password a Chrome-family browser keeps in the Secret Service.

    Returns ``(password, status)``; status is ``ok``, ``no-secret-service``,
    ``not-found``, ``locked`` or ``error``. A locked keyring stays locked unless
    ``unlock``: then the keyring itself asks the person (its own prompt on their
    desktop). lyra never sees or asks for the keyring's password.
    """
    try:
        import secretstorage
    except ImportError:
        return None, "no-secret-service"
    try:
        conn = secretstorage.dbus_init()
    except Exception:  # noqa: BLE001 - no session bus, no Secret Service
        return None, "no-secret-service"
    status = "not-found"
    try:
        for item in secretstorage.search_items(conn, {"application": application}):
            if item.is_locked() and unlock:
                item.unlock()  # the keyring's prompt; True means it was dismissed
            if item.is_locked():
                status = "locked"
                continue
            secret = item.get_secret()
            if secret:
                return bytes(secret), "ok"
    except Exception:  # noqa: BLE001 - a broken keyring is a missing one
        return None, "error"
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    return None, status


@dataclass
class CookieCrypto:
    """Opens the source's cookie values and seals them the way lyra's Chrome reads them.

    ``v11_passwords`` are tried in order; Chrome falls back to an empty password
    when its keyring hands it nothing, so ``b""`` is always the last candidate.
    """

    v11_passwords: list[bytes] = field(default_factory=list)
    _v11_keys: list[bytes] | None = None
    _v10_key: bytes = field(default_factory=lambda: _derive_key(_V10_PASSWORD))

    def _keys_for(self, prefix: bytes) -> list[bytes]:
        if prefix == b"v10":
            return [self._v10_key]
        if prefix == b"v11" and sys.platform.startswith("linux"):
            if self._v11_keys is None:
                self._v11_keys = [_derive_key(p) for p in [*self.v11_passwords, b""]]
            return self._v11_keys
        return []  # v10 on macOS/Windows is a different scheme; v20 is app-bound

    def decrypt(self, encrypted: bytes, host_key: str, schema: int) -> bytes | None:
        """The plaintext value, or None when no key this side has opens it."""
        prefix, body = encrypted[:3], encrypted[3:]
        for key in self._keys_for(prefix):
            try:
                plain = _unpad(_aes_cbc(key, body, encrypt=False))
            except ValueError:
                plain = None
            if plain is None:
                continue
            if schema >= _DOMAIN_HASH_SCHEMA:
                if plain[:32] != hashlib.sha256(host_key.encode()).digest():
                    continue  # the padding fooled us: wrong key
                return plain[32:]
            return plain
        return None

    def encrypt_v10(self, value: bytes, host_key: str, schema: int) -> bytes:
        if schema >= _DOMAIN_HASH_SCHEMA:
            value = hashlib.sha256(host_key.encode()).digest() + value
        return b"v10" + _aes_cbc(self._v10_key, _pad(value), encrypt=True)


# --------------------------------------------------------------------------- scope


@dataclass(slots=True)
class Scope:
    """Which sites an import touches. No ``sites`` means every site."""

    sites: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()

    @property
    def filtered(self) -> bool:
        """Whether some site is left out, so all-sites stores cannot be copied whole."""
        return bool(self.sites or self.denied)

    def wants_host(self, host: str) -> bool:
        """Whether a cookie for ``host`` (a ``host_key``, maybe ``.``-led) is in scope."""
        domain = host.lstrip(".").lower()
        if not domain:
            return False
        if self.sites and not any(_within(domain, site) for site in self.sites):
            return False
        return not self._denied_reaches(domain, host.startswith("."))

    def _denied_reaches(self, domain: str, any_subdomain: bool) -> bool:
        """Whether this cookie would be sent to a site the operator refused.

        A domain cookie (``.example.com``) goes to every host below it, so a
        refused ``a.example.com`` or ``*.example.com`` refuses it too.
        """
        if not self.denied:
            return False
        denied = parse_trusted_origins(self.denied)
        for host in (o.host for o in denied.origins):
            if host == domain or (any_subdomain and _within(host, domain)):
                return True
        for suffix in denied.suffixes:  # every host strictly below ``suffix``
            if domain != suffix and _within(domain, suffix):
                return True
            if any_subdomain and _within(suffix, domain):
                return True
        return False

    def wants_origin(self, origin: str) -> bool:
        """For storage keyed by origin (``https://a.example.com/``)."""
        host = _origin_host(origin)
        return bool(host) and self.wants_host(host)


def _within(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _origin_host(origin: str) -> str:
    """The host of an origin, a storage key, or an IndexedDB directory name."""
    text = origin.split("^", 1)[0]  # storage keys append ^0https://top-site
    if "://" in text:
        rest = text.split("://", 1)[1]
    elif "_" in text:  # "https_example.com_0.indexeddb.leveldb"
        scheme, _, rest = text.partition("_")
        if scheme not in ("http", "https"):
            return ""
        rest = rest.rsplit("_", 1)[0]
    else:
        return ""
    host = rest.split("/", 1)[0].rsplit(":", 1)[0].lower()
    if text.startswith(("chrome-extension:", "chrome:", "file:")):
        return ""
    return host


def _origin_is_web(origin: str) -> bool:
    return origin.startswith(("http://", "https://", "http_", "https_"))


# --------------------------------------------------------------------------- snapshots


def snapshot_sqlite(source: Path, dest: Path, *, attempts: int = 5) -> None:
    """A consistent copy of a SQLite file another process may be writing."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
            try:
                dst = sqlite3.connect(dest)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            return
        except sqlite3.Error as exc:
            last = exc
            time.sleep(0.2 * (attempt + 1))
    raise ImportError_(f"could not read {source}: {last}")


def _tree_signature(root: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        with contextlib.suppress(OSError):
            if path.is_file():
                st = path.stat()
                out[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def snapshot_tree(
    source: Path,
    dest: Path,
    *,
    keep: Callable[[Path], bool] = lambda _p: True,
    attempts: int = 5,
) -> None:
    """Copy a directory of LevelDB stores a live browser may be compacting.

    ``LOCK`` files are skipped: they belong to the process that holds the store.
    When a file vanishes mid-copy or the tree changed meanwhile, start over.
    """
    for _ in range(attempts):
        before = _tree_signature(source)
        if dest.exists():
            shutil.rmtree(dest)
        try:
            _copy_tree(source, dest, keep)
        except FileNotFoundError:
            continue
        if _tree_signature(source) == before:
            return
    raise ImportError_(f"{source} kept changing while it was copied; close the browser and retry")


def _copy_tree(source: Path, dest: Path, keep: Callable[[Path], bool]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name == "LOCK" or not keep(entry):
            continue
        target = dest / entry.name
        if entry.is_dir():
            _copy_tree(entry, target, lambda _p: True)
        elif entry.is_file():
            shutil.copy2(entry, target)


# --------------------------------------------------------------------------- cookies


@dataclass
class CookieReport:
    in_source: int = 0
    imported: int = 0
    out_of_scope: int = 0
    undecryptable: int = 0
    # Session cookies: Chrome deletes them when it starts (without session restore),
    # so lyra's would too. Left out and counted, never made persistent.
    session: int = 0
    replaced: int = 0
    kept: int = 0
    by_prefix: dict[str, int] = field(default_factory=dict)


def _schema(db: sqlite3.Connection) -> int:
    row = db.execute("select value from meta where key='version'").fetchone()
    return int(row[0]) if row else 0


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("pragma table_info(cookies)")]


def build_cookies(
    source_snapshot: Path,
    current: Path | None,
    out: Path,
    crypto: CookieCrypto,
    scope: Scope,
) -> CookieReport:
    """Write ``out``: lyra's cookies out of scope, plus the source's in scope, re-sealed.

    ``current`` is lyra's existing Cookies file (None for a fresh profile). Its
    schema must be the source's: Chrome migrates forward only, and rows from a
    newer schema in an older table would be read wrong.
    """
    report = CookieReport()
    src = sqlite3.connect(source_snapshot)
    try:
        schema = _schema(src)
        if current is not None:
            shutil.copy2(current, out)
            dst = sqlite3.connect(out)
            if _schema(dst) != schema:
                theirs = _schema(dst)
                dst.close()
                raise ImportError_(
                    f"lyra's cookie database is schema {theirs}, the source's is {schema}: "
                    "the browsers differ in version. Update the older one, or move "
                    "lyra's profile aside to start fresh."
                )
        else:
            shutil.copy2(source_snapshot, out)
            dst = sqlite3.connect(out)
            dst.execute("delete from cookies")
        try:
            cols = [c for c in _columns(src) if c in set(_columns(dst))]
            # Out-of-scope rows stay; in-scope rows are about to be replaced.
            existing = dst.execute("select rowid, host_key from cookies").fetchall()
            doomed = [rowid for rowid, host in existing if scope.wants_host(host)]
            dst.executemany("delete from cookies where rowid=?", [(r,) for r in doomed])
            report.replaced = len(doomed)
            report.kept = len(existing) - len(doomed)

            rows = src.execute(f"select {', '.join(cols)} from cookies").fetchall()
            i_host, i_enc = cols.index("host_key"), cols.index("encrypted_value")
            i_val = cols.index("value") if "value" in cols else None
            i_persist = cols.index("is_persistent") if "is_persistent" in cols else None
            keep: list[tuple] = []
            for row in rows:
                report.in_source += 1
                enc = bytes(row[i_enc] or b"")
                prefix = enc[:3].decode("latin-1") if enc else "plain"
                report.by_prefix[prefix] = report.by_prefix.get(prefix, 0) + 1
                host = row[i_host]
                if not scope.wants_host(host):
                    report.out_of_scope += 1
                    continue
                if i_persist is not None and not row[i_persist]:
                    report.session += 1
                    continue
                values = list(row)
                if enc:
                    plain = crypto.decrypt(enc, host, schema)
                    if plain is None:
                        report.undecryptable += 1
                        continue
                    values[i_enc] = crypto.encrypt_v10(plain, host, schema)
                    if i_val is not None:
                        values[i_val] = ""
                keep.append(tuple(values))
            marks = ", ".join("?" for _ in cols)
            dst.executemany(
                f"insert or replace into cookies ({', '.join(cols)}) values ({marks})", keep
            )
            report.imported = len(keep)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return report


# --------------------------------------------------------------------------- storage


def build_web_storage(source: Path, out: Path, scope: Scope) -> tuple[int, int]:
    """Copy the storage buckets of in-scope web origins. ``(kept, dropped)`` buckets."""
    snapshot_tree(
        source, out, keep=lambda p: p.name not in ("QuotaManager", "QuotaManager-journal")
    )
    quota = source / "QuotaManager"
    if not quota.exists():
        return 0, 0
    snapshot_sqlite(quota, out / "QuotaManager")
    db = sqlite3.connect(out / "QuotaManager")
    try:
        rows = db.execute("select id, storage_key from buckets").fetchall()
        dropped = [
            bucket_id
            for bucket_id, key in rows
            if not (_origin_is_web(key) and scope.wants_origin(key))
        ]
        db.executemany("delete from buckets where id=?", [(b,) for b in dropped])
        db.commit()
    finally:
        db.close()
    for bucket_id in dropped:
        with contextlib.suppress(OSError):
            shutil.rmtree(out / str(bucket_id))
    return len(rows) - len(dropped), len(dropped)


def _indexeddb_keep(scope: Scope) -> Callable[[Path], bool]:
    def keep(entry: Path) -> bool:
        return _origin_is_web(entry.name) and scope.wants_origin(entry.name)

    return keep


# --------------------------------------------------------------------------- import


@dataclass
class ImportResult:
    source: SourceProfile
    cookies: CookieReport
    components: dict[str, str]
    sidecar: dict
    # How the v11 key was found: "not-needed", "ok", "locked", "not-found", ...
    keyring: str = "not-needed"


_KEYRING_HINTS = {
    "locked": (
        "the source's cookies are sealed with a key in your login keyring, and the "
        "keyring is locked. Unlock it (log in to the desktop session), or re-run with "
        "--unlock-keyring to have the keyring ask you for its password."
    ),
    "not-found": (
        "the source's cookies are sealed with a keyring key (v11), but the keyring "
        "holds no 'Safe Storage' item for this browser."
    ),
    "no-secret-service": (
        "the source's cookies are sealed with a keyring key (v11), and no Secret "
        "Service is reachable on the session bus (DBUS_SESSION_BUS_ADDRESS)."
    ),
    "error": "the source's cookies are sealed with a keyring key, and the keyring failed.",
}

# Playwright channel for each source browser, so lyra opens the profile with the
# browser that wrote it: Chrome only migrates a profile forward, and a fallback to an
# older binary would meet databases from its future. Others have no channel to pin.
PINNED_CHANNELS = {"chrome": "chrome", "chrome-beta": "chrome-beta", "msedge": "msedge"}


def pinned_candidates(config: Config) -> list[str | None]:
    """The launch candidates, narrowed to the import's browser unless the operator chose."""
    candidates = config.browser_candidates()
    sidecar = read_sidecar(config)
    channel = PINNED_CHANNELS.get(str((sidecar or {}).get("browser")))
    if config.channel or channel is None:
        return candidates
    return [channel]


def _has_v11(cookies: Path) -> bool:
    """Whether the source holds keyring-sealed cookies, so the keyring is worth asking."""
    if not cookies.exists():
        return False
    try:
        db = sqlite3.connect(f"{cookies.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute(
                # A BLOB never equals TEXT in SQLite: 'v11' would match nothing.
                "select 1 from cookies where substr(encrypted_value, 1, 3) = x'763131' limit 1"
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return True  # cannot tell: let the import try, and count what fails
    return row is not None


def _profile_lock(config: Config):
    from .session import _PROFILE_LOCK_NAME, ProfileLock

    return ProfileLock(config.data_dir / _PROFILE_LOCK_NAME)


def import_profile(
    config: Config,
    source: SourceProfile,
    *,
    sites: Iterable[str] = (),
    crypto: CookieCrypto | None = None,
    dry_run: bool = False,
    unlock_keyring: bool = False,
) -> ImportResult:
    """Import (or re-sync) ``source`` into ``config``'s profile. See the module doc."""
    if not source.path.is_dir():
        raise ImportError_(f"{source.path} is not a profile directory")
    scope = Scope(
        sites=tuple(s.strip().lower().removeprefix("*.") for s in sites if s.strip()),
        denied=tuple(config.denied_origins),
    )
    keyring = "not-needed"
    if crypto is None:
        crypto = CookieCrypto()
        if sys.platform.startswith("linux") and _has_v11(source.path / "Cookies"):
            app = KEYRING_APPLICATIONS.get(source.browser, "chrome")
            password, keyring = read_keyring_password(app, unlock=unlock_keyring)
            if password:
                crypto.v11_passwords.append(password)
            elif not dry_run:
                # Importing the storage without the cookies would be half a login.
                raise ImportError_(_KEYRING_HINTS.get(keyring, _KEYRING_HINTS["error"]))

    config.data_dir.mkdir(parents=True, exist_ok=True)
    lock = _profile_lock(config)
    if not lock.try_acquire():
        raise ImportError_(
            "a lyra browser is running on this profile; close it (close_browser, or stop "
            "the server) and run the import again"
        )
    try:
        return _import_locked(config, source, scope, crypto, dry_run, keyring)
    finally:
        lock.release()


def _import_locked(
    config: Config,
    source: SourceProfile,
    scope: Scope,
    crypto: CookieCrypto,
    dry_run: bool,
    keyring: str,
) -> ImportResult:
    staging = config.data_dir / _STAGING_NAME
    retired = config.data_dir / _RETIRED_NAME
    target = config.profile_dir / TARGET_PROFILE
    _recover(retired, target)  # a swap a killed import left half-way
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    components: dict[str, str] = {}
    try:
        cookies = CookieReport()
        src_cookies = source.path / "Cookies"
        if src_cookies.exists():
            snap = staging / "source-Cookies"
            snapshot_sqlite(src_cookies, snap)
            current = target / "Cookies"
            cookies = build_cookies(
                snap, current if current.exists() else None, staging / "Cookies", crypto, scope
            )
            snap.unlink()
            components["Cookies"] = f"{cookies.imported} imported"
        else:
            components["Cookies"] = "absent in source"

        if not (source.path / "Local Storage").is_dir():
            components["Local Storage"] = "absent in source"
        elif scope.filtered:
            components["Local Storage"] = "skipped: it cannot be filtered by site"
        else:
            snapshot_tree(source.path / "Local Storage", staging / "Local Storage")
            components["Local Storage"] = "copied"

        if (source.path / "IndexedDB").is_dir():
            keep = _indexeddb_keep(scope)
            snapshot_tree(source.path / "IndexedDB", staging / "IndexedDB", keep=keep)
            n = sum(1 for _ in (staging / "IndexedDB").iterdir())
            components["IndexedDB"] = f"{n} origin stores"
        else:
            components["IndexedDB"] = "absent in source"

        if (source.path / "WebStorage").is_dir():
            kept, dropped = build_web_storage(
                source.path / "WebStorage", staging / "WebStorage", scope
            )
            components["WebStorage"] = f"{kept} buckets ({dropped} out of scope)"
        else:
            components["WebStorage"] = "absent in source"

        sidecar = {
            "browser": source.browser,
            "user_data_dir": str(source.user_data_dir),
            "profile": source.directory,
            "name": source.name,
            "source_version": source_version(source.user_data_dir),
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "sites": list(scope.sites),
            "keyring": keyring,
            "cookies": dataclasses.asdict(cookies),
            "components": components,
        }
        if dry_run:
            return ImportResult(source, cookies, components, sidecar, keyring)

        _swap_in(staging, target, retired)
        _write_atomic(config.data_dir / SIDECAR_NAME, json.dumps(sidecar, indent=2) + "\n")
        return ImportResult(source, cookies, components, sidecar, keyring)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


_COMPONENTS = ("Cookies", "Local Storage", "IndexedDB", "WebStorage")
# Beside the cookie file: a journal left there would be rolled into the new file.
_COOKIE_SIDE_FILES = ("Cookies-journal", "Cookies-wal", "Cookies-shm")


def _swap_in(staging: Path, target: Path, retired: Path) -> None:
    """Move each staged component into ``target``; on any failure put the old ones back.

    The replaced files wait in ``retired`` until every component is in. A process
    killed half-way leaves them there, and the next import first puts back each one
    whose new version never arrived (``_recover``): every component is then either
    the old one or the complete new one, never a mix within one.
    """
    target.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(target.parent, 0o700)
    retired.mkdir()
    moved: list[str] = []  # names now in ``retired``
    placed: list[str] = []  # names now in ``target`` from ``staging``
    try:
        for name in _COMPONENTS:
            staged = staging / name
            if not staged.exists():
                continue
            outgoing = [name, *(_COOKIE_SIDE_FILES if name == "Cookies" else ())]
            for old in outgoing:
                if (target / old).exists():
                    os.replace(target / old, retired / old)
                    moved.append(old)
            os.replace(staged, target / name)
            placed.append(name)
    except BaseException:
        for name in placed:
            _remove(target / name)
        _recover(retired, target)
        raise
    shutil.rmtree(retired, ignore_errors=True)


def _recover(retired: Path, target: Path) -> None:
    """Put back what an interrupted swap had moved aside, then forget the swap.

    Entry by entry, so one that will not move does not strand the others; whatever
    could not be restored stays in ``retired`` for the next attempt, never deleted.
    """
    if not retired.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for entry in list(retired.iterdir()):
        if (target / entry.name).exists():
            _remove(entry)  # the swap never got as far as replacing this one
            continue
        with contextlib.suppress(OSError):
            os.replace(entry, target / entry.name)
    with contextlib.suppress(OSError):
        retired.rmdir()  # only when empty


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_sidecar(config: Config) -> dict | None:
    """What the last import recorded, or None if this profile was never imported into."""
    try:
        data = json.loads((config.data_dir / SIDECAR_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


__all__ = [
    "CookieCrypto",
    "ImportError_",
    "ImportResult",
    "Scope",
    "SourceProfile",
    "browser_roots",
    "import_profile",
    "list_profiles",
    "pinned_candidates",
    "read_sidecar",
    "resolve_profile",
]
