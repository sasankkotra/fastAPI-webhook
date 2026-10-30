"""Parameterized SQL against the existing table only. No schema management."""

import hashlib
import json

from psycopg import AsyncConnection, IsolationLevel
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from config import Settings
from models import FormSubmission, SubmissionResult


class EventConflict(Exception):
    """An event ID was reused with different data."""


async def configure_connection(connection: AsyncConnection) -> None:
    await connection.set_isolation_level(IsolationLevel.READ_COMMITTED)


def create_pool(settings: Settings) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo="",
        kwargs={
            "host": settings.db_host,
            "port": settings.db_port,
            "dbname": settings.db_name,
            "user": settings.db_user,
            "password": settings.db_password.get_secret_value(),
            "sslmode": settings.db_sslmode,
            "connect_timeout": 5,
            "application_name": "wordpress-webhook",
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms} -c lock_timeout=5000",
            "row_factory": dict_row,
        },
        min_size=0,
        max_size=settings.db_pool_max_size,
        max_waiting=40,
        timeout=settings.db_pool_timeout_seconds,
        open=False,
        configure=configure_connection,
        check=AsyncConnectionPool.check_connection,
    )


async def insert_submission(pool: AsyncConnectionPool, submission: FormSubmission) -> SubmissionResult:
    # The connection context commits before returning; exceptions roll back.
    async with pool.connection() as connection:
        event_id = submission.formData.get("_event_id")
        if event_id is not None:
            identity = json.dumps(
                ["ordr.wordpress.forms.v1", submission.formId, submission.postId, event_id],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            lock_key = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big", signed=True)
            # A separate SELECT after acquiring the lock sees the preceding
            # transaction's commit under READ COMMITTED, across all replicas.
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            cursor = await connection.execute(
                """SELECT id, created_at, form_data = %s AS same_payload
                   FROM public.wordpress_form_raw
                   WHERE form_id = %s AND post_id IS NOT DISTINCT FROM %s
                     AND form_data ->> '_event_id' = %s
                   ORDER BY created_at, id LIMIT 1""",
                (Jsonb(submission.formData), submission.formId, submission.postId, event_id),
            )
            existing = await cursor.fetchone()
            if existing:
                if not existing["same_payload"]:
                    raise EventConflict
                return SubmissionResult(
                    id=existing["id"], created_at=existing["created_at"], duplicate=True
                )

        cursor = await connection.execute(
            """INSERT INTO public.wordpress_form_raw (form_id, post_id, form_data)
               VALUES (%s, %s, %s) RETURNING id, created_at""",
            (submission.formId, submission.postId, Jsonb(submission.formData)),
        )
        row = await cursor.fetchone()
        return SubmissionResult(id=row["id"], created_at=row["created_at"])
