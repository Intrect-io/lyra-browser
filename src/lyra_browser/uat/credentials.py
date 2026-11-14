"""Logins a persona can use without ever seeing them.

A run spec names a credential and where its secrets live (an environment variable or a
file), never the secrets. The persona calls ``fill_credential(name, field, selector)``;
the server resolves the value, checks that the page is on the credential's own origin,
and types it through ``type_text`` — so the same consent, enforcement and audit
redaction apply as to anything typed. The tool's arguments, the trace, the report and
the result the model reads carry the name and the field only. ``totp`` is computed at
the moment it is typed from the seed (RFC 6238); the seed itself is never typed.

Everything resolved here is also handed to the run's ``Scrubber``, so a page that echoes
a password, or a captured POST body that carries it, is written with the value replaced.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..origin import parse_origin
from ..totp import decode_secret, totp
from .spec import CredentialRef

Field = Literal["username", "password", "totp"]
FIELDS: tuple[str, ...] = ("username", "password", "totp")


@dataclass
class Credential:
    name: str
    origin: str  # parse_origin(...).describe()
    username: str
    password: str | None
    totp_secret: str | None
    problem: str | None = None  # why a part could not be read

    def value(self, field: str) -> str | None:
        if field == "username":
            return self.username or None
        if field == "password":
            return self.password
        if field == "totp":
            return totp(self.totp_secret) if self.totp_secret else None
        return None


def _read(env: str | None, file: Path | None, what: str) -> tuple[str | None, str | None]:
    if env:
        value = os.environ.get(env)
        if not value:
            return None, f"{what}: environment variable {env} is not set"
        return value, None
    if file:
        try:
            return file.expanduser().read_text(encoding="utf-8").strip(), None
        except OSError as exc:
            return None, f"{what}: cannot read {file}: {exc.strerror}"
    return None, None


def resolve(name: str, ref: CredentialRef) -> Credential:
    """Read the secrets now. A missing one is a ``problem`` reported when it is asked
    for, not an exception: the run goes on and the persona is told what is missing."""
    problems = []
    username, problem = _read(ref.username_env, None, "username")
    if problem:
        problems.append(problem)
    password, problem = _read(ref.password_env, ref.password_file, "password")
    if problem:
        problems.append(problem)
    seed, problem = _read(ref.totp_env, ref.totp_file, "totp")
    if problem:
        problems.append(problem)
    if seed:
        try:
            decode_secret(seed)
        except ValueError:
            problems.append("totp: the seed is not base32")
            seed = None
    return Credential(
        name=name,
        origin=parse_origin(ref.origin).describe(),
        username=username or ref.username,
        password=password,
        totp_secret=seed,
        problem="; ".join(problems) or None,
    )


class CredentialStore:
    def __init__(self, refs: dict[str, CredentialRef]) -> None:
        self.credentials = {name: resolve(name, ref) for name, ref in refs.items()}

    def secrets(self) -> list[str]:
        """What must never be written or shown: passwords and TOTP seeds (not usernames,
        and not the six-digit codes, which would match ordinary numbers)."""
        out = []
        for cred in self.credentials.values():
            out += [s for s in (cred.password, cred.totp_secret) if s]
        return out


def fill_envelope(store: CredentialStore, name: str, field: str) -> tuple[Any, dict | None]:
    """The value to type, or the envelope that refuses (never containing a value)."""
    cred = store.credentials.get(name)
    if cred is None:
        return None, {
            "status": "error",
            "reason": "unknown credential",
            "known": sorted(store.credentials),
        }
    if field not in FIELDS:
        return None, {"status": "error", "reason": f"field must be one of {list(FIELDS)}"}
    value = cred.value(field)
    if not value:
        return None, {
            "status": "credential_unavailable",
            "credential": name,
            "field": field,
            "reason": cred.problem or f"credential {name!r} has no {field}",
        }
    return (cred, value), None
