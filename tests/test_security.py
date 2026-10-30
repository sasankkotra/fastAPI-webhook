import hashlib
import hmac
import time

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from conftest import SECRET
from security import decode_webhook_secret, verify_signature


def test_key_is_exactly_2048_bits_and_signature_is_256_bits():
    key = decode_webhook_secret(SECRET)
    assert len(key) * 8 == 2048
    assert len(hmac.new(key, b"message", hashlib.sha256).digest()) * 8 == 256


@pytest.mark.parametrize("secret", [
    "a" * 32, "ab" * 32, "ab" * 255, "ab" * 257,
    "g" * 512, " " * 512, "ab" * 256 + "\n",
])
def test_settings_reject_short_long_or_nonhex_keys(settings, secret):
    values = settings.model_dump()
    values["webhook_secret"] = secret
    with pytest.raises(ValidationError):
        type(settings)(_env_file=None, **values)


def test_uppercase_hex_is_accepted(settings):
    values = settings.model_dump()
    values["webhook_secret"] = SECRET.upper()
    configured = type(settings)(_env_file=None, **values)
    assert decode_webhook_secret(configured.webhook_secret.get_secret_value()) == bytes(range(256))


def test_sender_must_hex_decode_the_key():
    body = b'{"formId":"101","formData":{}}'
    timestamp = str(int(time.time()))
    message = timestamp.encode() + b"." + body
    correct = "sha256=" + hmac.new(bytes.fromhex(SECRET), message, hashlib.sha256).hexdigest()
    verify_signature(body, timestamp, correct, SECRET, 300)
    incorrect = "sha256=" + hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException) as failure:
        verify_signature(body, timestamp, incorrect, SECRET, 300)
    assert failure.value.status_code == 401
