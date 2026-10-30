import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import main
from config import Settings

# Deterministic test fixture only. Production keys must be randomly generated.
SECRET = bytes(range(256)).hex()
ROW = {"id": UUID("8f2eb8be-bf38-496d-8557-3c448dc183ad"), "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
PAYLOAD = {"formId": "101", "postId": "5001", "formData": {"establishment_name": "Sri Udupi Park", "city": "Bengaluru"}}


def signed_headers(body: bytes, timestamp: str | None = None, secret: str = SECRET) -> dict:
    timestamp = timestamp if timestamp is not None else str(int(time.time()))
    digest = hmac.new(bytes.fromhex(secret), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Webhook-Timestamp": timestamp, "X-Webhook-Signature": "sha256=" + digest}


def encode(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


@pytest.fixture
def settings():
    return Settings(_env_file=None, webhook_secret=SECRET, db_host="localhost", db_name="test", db_user="test", db_password="test")


class FakePool:
    def __init__(self):
        self.conn = AsyncMock()
        self.conn.execute.return_value.fetchone.return_value = ROW.copy()
        self.open = AsyncMock()
        self.close = AsyncMock()

    @asynccontextmanager
    async def connection(self):
        yield self.conn


@pytest.fixture
def pool():
    return FakePool()


@pytest.fixture
def client(monkeypatch, settings, pool):
    monkeypatch.setattr(main, "create_pool", lambda _: pool)
    with TestClient(main.create_app(settings)) as client:
        yield client
