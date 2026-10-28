"""The macOS side of the profile import: Keychain-sealed v10 in, mock-keychain v10 out.

Runs on any platform: the crypto is told it is on macOS, and ``security`` is faked.
The keys were measured against Chrome 156 on macOS (2026-10-28).
"""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from lyra_browser import profile_import
from lyra_browser.profile_import import CookieCrypto, read_keychain_password

SCHEMA = 24
KEYCHAIN = b"c2FmZS1zdG9yYWdlLXNlY3JldA=="


def seal_mac(value: bytes, host: str, password: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    value = hashlib.sha256(host.encode()).digest() + value
    n = 16 - len(value) % 16
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + enc.update(value + bytes([n]) * n) + enc.finalize()


def mac(*passwords: bytes) -> CookieCrypto:
    return CookieCrypto(list(passwords), platform="darwin")


def test_a_keychain_sealed_cookie_opens_with_the_keychain_password():
    sealed = seal_mac(b"token", "example.com", KEYCHAIN)
    assert mac(KEYCHAIN).decrypt(sealed, "example.com", SCHEMA) == b"token"
    assert mac().decrypt(sealed, "example.com", SCHEMA) is None


def test_the_reseal_is_what_playwrights_mock_keychain_reads():
    sealed = mac(KEYCHAIN).encrypt_v10(b"token", "example.com", SCHEMA)
    assert sealed == seal_mac(b"token", "example.com", b"mock_password")


def test_lyras_own_cookies_still_open_after_a_sync():
    own = seal_mac(b"mine", "mine.test", b"mock_password")
    assert mac(KEYCHAIN).decrypt(own, "mine.test", SCHEMA) == b"mine"


def test_the_linux_keys_are_not_tried_on_macos():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, dklen=16)
    plain = hashlib.sha256(b"a.test").digest() + b"x" + bytes([15]) * 15
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    sealed = b"v10" + enc.update(plain) + enc.finalize()
    assert mac().decrypt(sealed, "a.test", SCHEMA) is None


@pytest.mark.parametrize(
    "returncode,stdout,stderr,status",
    [
        (0, KEYCHAIN + b"\n", b"", "ok"),
        (44, b"", b"The specified item could not be found in the keychain.", "not-found"),
        (36, b"", b"User interaction is not allowed.", "locked"),
        (128, b"", b"", "denied"),
        (1, b"", b"something else", "error"),
    ],
)
def test_keychain_answers_map_to_a_status(monkeypatch, returncode, stdout, stderr, status):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(profile_import.subprocess, "run", run)
    password, got = read_keychain_password("msedge")
    assert got == status
    assert password == (KEYCHAIN if status == "ok" else None)
    assert calls[0][-4:] == ["-s", "Microsoft Edge Safe Storage", "-a", "Microsoft Edge"]
