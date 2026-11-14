"""Time-based one-time passwords (RFC 6238), from the standard library alone.

What an authenticator app shows for a shared secret: HMAC of the number of ``period``
second steps since the epoch, dynamically truncated (RFC 4226) to ``digits`` digits.
Secrets are base32 as sites hand them out (spaces and lower case accepted, padding
optional).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

_DIGESTS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}


def decode_secret(secret: str) -> bytes:
    cleaned = "".join(secret.split()).upper()
    cleaned += "=" * (-len(cleaned) % 8)
    try:
        return base64.b32decode(cleaned, casefold=True)
    except (ValueError, TypeError) as exc:  # binascii.Error is a ValueError
        raise ValueError("a TOTP secret must be base32") from exc


def hotp(key: bytes, counter: int, *, digits: int = 6, algorithm: str = "sha1") -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), _DIGESTS[algorithm]).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**digits).zfill(digits)


def totp(
    secret: str | bytes,
    *,
    at: float | None = None,
    period: int = 30,
    digits: int = 6,
    algorithm: str = "sha1",
) -> str:
    """The code for ``secret`` at time ``at`` (now by default)."""
    key = decode_secret(secret) if isinstance(secret, str) else secret
    now = time.time() if at is None else at
    return hotp(key, int(now // period), digits=digits, algorithm=algorithm)


def verify(secret: str, code: str, *, at: float | None = None, window: int = 1) -> bool:
    """Whether ``code`` is valid now, or within ``window`` steps either side (clock skew)."""
    now = time.time() if at is None else at
    return any(
        hmac.compare_digest(totp(secret, at=now + step * 30), code)
        for step in range(-window, window + 1)
    )
