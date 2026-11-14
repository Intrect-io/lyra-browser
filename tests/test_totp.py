"""TOTP against the RFC 6238 appendix B test vectors."""

from __future__ import annotations

import base64

import pytest

from lyra_browser.totp import decode_secret, totp, verify

# RFC 6238 appendix B: the seeds are the ASCII strings below, 8-digit codes.
SEEDS = {
    "sha1": b"12345678901234567890",
    "sha256": b"12345678901234567890123456789012",
    "sha512": b"1234567890123456789012345678901234567890123456789012345678901234",
}
VECTORS = [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]


@pytest.mark.parametrize(("at", "sha1", "sha256", "sha512"), VECTORS)
def test_rfc6238_vectors(at, sha1, sha256, sha512):
    for algorithm, expected in (("sha1", sha1), ("sha256", sha256), ("sha512", sha512)):
        assert totp(SEEDS[algorithm], at=at, digits=8, algorithm=algorithm) == expected


def test_a_base32_secret_as_sites_print_it():
    secret = base64.b32encode(SEEDS["sha1"]).decode().rstrip("=").lower()
    spaced = " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
    assert decode_secret(spaced) == SEEDS["sha1"]
    assert totp(spaced, at=59, digits=8) == "94287082"
    assert len(totp(spaced)) == 6


def test_verify_allows_one_step_of_skew_and_no_more():
    secret = base64.b32encode(SEEDS["sha1"]).decode()
    code = totp(secret, at=1_000_000)
    assert verify(secret, code, at=1_000_000)
    assert verify(secret, code, at=1_000_000 + 30)
    assert not verify(secret, code, at=1_000_000 + 90)


def test_a_secret_that_is_not_base32_is_refused():
    with pytest.raises(ValueError):
        decode_secret("not base32 at all!")
