"""Authenticate the exact bytes, including a signed Unix timestamp."""

import hashlib
import hmac
import re
import time

from fastapi import HTTPException


def decode_webhook_secret(secret: str) -> bytes:
    """Decode exactly 256 key bytes (2048 bits) from their hex representation."""
    if re.fullmatch(r"[0-9a-fA-F]{512}", secret) is None:
        raise ValueError("WEBHOOK_SECRET must encode a 2048-bit key as exactly 512 hexadecimal characters")
    return bytes.fromhex(secret)


def verify_signature(
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    tolerance: int,
) -> None:
    if (
        timestamp is None
        or re.fullmatch(r"[0-9]{1,12}", timestamp) is None
        or signature is None
        or re.fullmatch(r"sha256=[0-9a-f]{64}", signature) is None
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook authentication")
    if abs(time.time() - int(timestamp)) > tolerance:
        raise HTTPException(status_code=401, detail="Invalid webhook authentication")
    expected = hmac.new(
        decode_webhook_secret(secret), timestamp.encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature[7:], expected):
        raise HTTPException(status_code=401, detail="Invalid webhook authentication")
