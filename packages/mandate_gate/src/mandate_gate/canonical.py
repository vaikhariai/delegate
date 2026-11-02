"""Canonical JSON serialization and action hashing (rule ``delegate-json-v1``).

The rule: UTF-8 bytes of ``json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
allow_nan=False)`` where every number is an ``int`` (floats are rejected outright). Strings are kept exactly
as given; no Unicode normalization is applied. The hash is the lowercase hex SHA-256 of those bytes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

CANONICAL_VERSION = "delegate-json-v1"


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented under the canonical rule."""


def _check(value: Any, path: str) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        raise CanonicalizationError(f"float at {path or '$'}: use integer minor units")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"non-string key at {path or '$'}")
            _check(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check(item, f"{path}[{index}]")
        return
    raise CanonicalizationError(f"unsupported type {type(value).__name__} at {path or '$'}")


def canonical_bytes(value: Any) -> bytes:
    """Return the canonical UTF-8 bytes for a JSON-compatible value containing only integer numbers."""
    _check(value, "")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def action_hash(value: Any) -> str:
    """Return the hex SHA-256 digest of the canonical bytes of ``value``."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
