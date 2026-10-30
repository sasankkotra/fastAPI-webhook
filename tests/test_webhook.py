import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from conftest import PAYLOAD, ROW, SECRET, encode, signed_headers

PATH = "/webhooks/wordpress/forms"


def test_insert_returns_database_generated_values_and_preserves_json(client, pool):
    payload = {**PAYLOAD, "formData": {"nested": {"list": [1, True, None, "ಬೆಂಗಳೂರು"]}, "empty": {}}}
    body = encode(payload)
    response = client.post(PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 201
    assert response.json() == {"id": "8f2eb8be-bf38-496d-8557-3c448dc183ad", "created_at": "2026-01-01T00:00:00Z", "duplicate": False}
    sql, parameters = pool.conn.execute.call_args.args
    assert "INSERT INTO public.wordpress_form_raw (form_id, post_id, form_data)" in sql
    assert "RETURNING id, created_at" in sql
    assert parameters[:2] == ("101", "5001")
    assert parameters[2].obj == payload["formData"]


def test_same_identifiers_without_event_id_always_insert(client, pool):
    body = encode(PAYLOAD)
    for _ in range(2):
        assert client.post(PATH, content=body, headers=signed_headers(body)).status_code == 201
    assert pool.conn.execute.call_count == 2


def test_retry_response_and_conflicting_event(client, pool):
    body = encode({**PAYLOAD, "formData": {"_event_id": "event-1"}})
    pool.conn.execute.side_effect = [AsyncMock(), AsyncMock(fetchone=AsyncMock(return_value={**ROW, "same_payload": True}))]
    response = client.post(PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    pool.conn.execute.side_effect = [AsyncMock(), AsyncMock(fetchone=AsyncMock(return_value={**ROW, "same_payload": False}))]
    assert client.post(PATH, content=body, headers=signed_headers(body)).status_code == 409


@pytest.mark.parametrize("post_id", [None, "separate-post"])
def test_optional_post_id(client, pool, post_id):
    body = encode({"formId": "101", "postId": post_id, "formData": {}})
    assert client.post(PATH, content=body, headers=signed_headers(body)).status_code == 201
    assert pool.conn.execute.call_args.args[1][1] == post_id


@pytest.mark.parametrize("headers", [
    {},
    {"X-Webhook-Timestamp": "abc", "X-Webhook-Signature": "sha256=" + "a" * 64},
    {"X-Webhook-Timestamp": "1", "X-Webhook-Signature": "invalid"},
])
def test_rejects_missing_or_malformed_authentication(client, pool, headers):
    response = client.post(PATH, content=encode(PAYLOAD), headers=headers)
    assert response.status_code == 401
    pool.conn.execute.assert_not_called()


@pytest.mark.parametrize("offset", [-301, 301])
def test_rejects_expired_and_future_timestamp(client, pool, offset):
    body = encode(PAYLOAD)
    timestamp = str(int(time.time()) + offset)
    assert client.post(PATH, content=body, headers=signed_headers(body, timestamp)).status_code == 401
    pool.conn.execute.assert_not_called()


def test_rejects_wrong_secret_tampered_body_and_tampered_timestamp(client, pool):
    body = encode(PAYLOAD)
    headers = signed_headers(body)
    assert client.post(PATH, content=body + b" ", headers=headers).status_code == 401
    assert client.post(PATH, content=body, headers=signed_headers(body, secret="ff" * 256)).status_code == 401
    headers["X-Webhook-Timestamp"] = str(int(headers["X-Webhook-Timestamp"]) - 1)
    assert client.post(PATH, content=body, headers=headers).status_code == 401
    pool.conn.execute.assert_not_called()


@pytest.mark.parametrize("payload", [
    {}, {**PAYLOAD, "formId": 101}, {**PAYLOAD, "formId": " "},
    {**PAYLOAD, "formId": "a" * 256}, {**PAYLOAD, "postId": 5001},
    {**PAYLOAD, "formData": []}, {**PAYLOAD, "formData": None},
    {**PAYLOAD, "unexpected": "value"}, {**PAYLOAD, "formData": {"_event_id": 1}},
    {**PAYLOAD, "formData": {"_event_id": " "}},
    {**PAYLOAD, "formData": {"nested": ["\x00"]}},
    {**PAYLOAD, "formData": {"bad": "\ud800"}},
])
def test_rejects_invalid_payload_without_echoing_input(client, pool, payload):
    # ASCII escaping also permits testing an unpaired Unicode surrogate.
    body = json.dumps(payload).encode()
    response = client.post(PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 422
    assert "Sri Udupi Park" not in response.text
    pool.conn.execute.assert_not_called()


@pytest.mark.parametrize("body", [b"{", b'{"a":NaN}', b'{"a":1e9999}', b'{"formId":"1","formId":"2"}', b"\xff"])
def test_invalid_json(client, body):
    assert client.post(PATH, content=body, headers=signed_headers(body)).status_code == 400


def test_wrong_media_type(client):
    body = encode(PAYLOAD)
    headers = signed_headers(body)
    headers["Content-Type"] = "text/plain"
    assert client.post(PATH, content=body, headers=headers).status_code == 415


def test_body_limit_including_chunked_request(client, settings, pool):
    settings.max_request_bytes = 16
    assert client.post(PATH, content=iter([b"a" * 10, b"b" * 10])).status_code == 413
    pool.conn.execute.assert_not_called()


def test_database_failure_is_sanitized_and_retryable(client, pool, caplog):
    pool.conn.execute.side_effect = psycopg.OperationalError("sensitive SQL and password")
    body = encode(PAYLOAD)
    with caplog.at_level("INFO", logger="wordpress_webhook"):
        response = client.post(PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "sensitive" not in response.text + caplog.text
    assert SECRET not in caplog.text
    assert "Sri Udupi Park" not in caplog.text


def test_commit_failure_never_reports_success(client, pool, monkeypatch):
    @asynccontextmanager
    async def failed_commit():
        yield pool.conn
        raise psycopg.OperationalError("commit acknowledgement lost")

    monkeypatch.setattr(pool, "connection", failed_commit)
    body = encode(PAYLOAD)
    assert client.post(PATH, content=body, headers=signed_headers(body)).status_code == 503


def test_exhausted_pool_returns_retryable_error(client, pool, monkeypatch):
    @asynccontextmanager
    async def exhausted_pool():
        raise PoolTimeout("no connection available")
        yield  # Keep this an async context manager.

    monkeypatch.setattr(pool, "connection", exhausted_pool)
    body = encode(PAYLOAD)
    assert client.post(PATH, content=body, headers=signed_headers(body)).status_code == 503


def test_health(client, pool):
    assert client.get("/health").json() == {"status": "ok"}
    assert pool.conn.execute.call_args.args == ("SELECT 1",)
    pool.conn.execute.side_effect = psycopg.OperationalError("internal details")
    response = client.get("/health")
    assert response.status_code == 503
    assert "internal details" not in response.text
