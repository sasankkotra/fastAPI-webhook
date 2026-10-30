from unittest.mock import AsyncMock

import pytest

from conftest import PAYLOAD, ROW
from database import EventConflict, insert_submission
from models import FormSubmission


async def test_event_retry_returns_original_without_inserting(pool):
    pool.conn.execute.side_effect = [AsyncMock(), AsyncMock(fetchone=AsyncMock(return_value={**ROW, "same_payload": True}))]
    submission = FormSubmission(**{**PAYLOAD, "formData": {"_event_id": "event-1", "city": "Bengaluru"}})
    result = await insert_submission(pool, submission)
    assert result.duplicate and result.id == ROW["id"]
    assert pool.conn.execute.call_count == 2
    assert "pg_advisory_xact_lock" in pool.conn.execute.call_args_list[0].args[0]
    assert pool.conn.execute.call_args_list[1].args[1][1:] == ("101", "5001", "event-1")


async def test_new_event_locks_checks_and_inserts(pool):
    pool.conn.execute.side_effect = [AsyncMock(), AsyncMock(fetchone=AsyncMock(return_value=None)), AsyncMock(fetchone=AsyncMock(return_value=ROW))]
    submission = FormSubmission(**{**PAYLOAD, "formData": {"_event_id": "new-event"}})
    result = await insert_submission(pool, submission)
    assert not result.duplicate
    assert "INSERT" in pool.conn.execute.call_args.args[0]


async def test_event_id_with_different_data_conflicts(pool):
    pool.conn.execute.side_effect = [AsyncMock(), AsyncMock(fetchone=AsyncMock(return_value={**ROW, "same_payload": False}))]
    with pytest.raises(EventConflict):
        await insert_submission(pool, FormSubmission(**{**PAYLOAD, "formData": {"_event_id": "used"}}))
    assert pool.conn.execute.call_count == 2
