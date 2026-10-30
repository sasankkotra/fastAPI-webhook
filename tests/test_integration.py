"""Opt-in tests against a PREEXISTING table in a disposable test database.

No DDL. Never point TEST_DATABASE_DSN at production. Only rows bearing the
random test form_id are deleted by fixture teardown.
"""

import asyncio
import os
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from database import EventConflict, configure_connection, insert_submission
from models import FormSubmission

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv("TEST_DATABASE_DSN"), reason="TEST_DATABASE_DSN is not set")]


@pytest.fixture
async def real_database():
    form_id = "ordr-webhook-test-" + str(uuid4())
    async with AsyncConnectionPool(
        os.environ["TEST_DATABASE_DSN"], min_size=1, max_size=5,
        timeout=5, open=False, kwargs={"row_factory": dict_row},
        configure=configure_connection,
    ) as pool:
        try:
            yield pool, form_id
        finally:
            async with pool.connection() as connection:
                await connection.execute("DELETE FROM public.wordpress_form_raw WHERE form_id = %s", (form_id,))


async def test_real_insert_and_submission_history(real_database):
    pool, form_id = real_database
    data = {"nested": [None, True, {"city": "ಬೆಂಗಳೂರು"}], "empty": {}}
    submission = FormSubmission(formId=form_id, postId="5001", formData=data)
    first = await insert_submission(pool, submission)
    second = await insert_submission(pool, submission)
    assert first.id != second.id
    assert first.created_at.tzinfo is not None
    async with pool.connection() as connection:
        rows = await (await connection.execute("SELECT * FROM public.wordpress_form_raw WHERE form_id = %s", (form_id,))).fetchall()
    assert len(rows) == 2
    assert all(row["post_id"] == "5001" and row["form_data"] == data for row in rows)


async def test_concurrent_retries_and_legitimate_update(real_database):
    pool, form_id = real_database
    submission = FormSubmission(formId=form_id, formData={"_event_id": "event-1", "city": "Bengaluru"})
    results = await asyncio.gather(*(insert_submission(pool, submission) for _ in range(5)))
    assert len({row.id for row in results}) == 1
    assert sum(not row.duplicate for row in results) == 1
    with pytest.raises(EventConflict):
        await insert_submission(pool, FormSubmission(formId=form_id, formData={"_event_id": "event-1", "city": "Mumbai"}))
    updated = await insert_submission(pool, FormSubmission(formId=form_id, formData={"_event_id": "event-2", "city": "Mumbai"}))
    assert updated.id != results[0].id
    async with pool.connection() as connection:
        row = await (await connection.execute("SELECT count(*) AS count FROM public.wordpress_form_raw WHERE form_id = %s", (form_id,))).fetchone()
    assert row["count"] == 2
