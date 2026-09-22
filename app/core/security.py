"""Security utilities: safe hashing of identifiers.

The raw ``payload_id`` is a correlation key that may itself be sensitive.
We never log it directly; we log a truncated hash instead.
"""

from __future__ import annotations

import hashlib


def hash_identifier(value: str, *, length: int = 12) -> str:
    """Return a short, stable, non-reversible hash of an identifier.

    Suitable for logging and metrics labels. The output is not meant to be
    cryptographically strong for authentication, only to avoid leaking the
    raw identifier into logs.
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:length]


def hash_payload(value: str) -> str:
    """Return a full SHA-256 hex digest of a payload.

    Used to detect whether a retry carries the same payload without storing
    or logging the payload itself.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()