"""ORDR.ai WordPress receiver. Configuration is loaded at application startup."""

import json
import logging
import math
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import ValidationError
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from config import Settings
from database import EventConflict, create_pool, insert_submission
from models import FormSubmission, SubmissionResult
from security import verify_signature

logger = logging.getLogger("wordpress_webhook")
DATABASE_ERRORS = (psycopg.Error, PoolTimeout, PoolClosed, TooManyRequests)


def log_event(event: str, **fields: object) -> None:
    logger.info(json.dumps({"event": event, **fields}))


def parse_json(body: bytes) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError("Nonstandard JSON number")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Nonfinite JSON number")
        return number

    def unique_keys(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    # Explicit UTF-8 rather than json.loads(bytes)'s UTF-16/32 autodetection.
    return json.loads(
        body.decode("utf-8"), parse_constant=reject_constant,
        parse_float=finite_float, object_pairs_hook=unique_keys,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        # Pool exception strings can include connection details. App failures
        # below log the error class only, never payloads, SQL or credentials.
        logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)
        application.state.settings = settings or Settings()
        pool = create_pool(application.state.settings)
        application.state.pool = pool
        await pool.open()
        try:
            yield
        finally:
            await pool.close()

    application = FastAPI(title="ORDR.ai WordPress Webhook", lifespan=lifespan)

    @application.get("/health")
    async def health() -> dict[str, str]:
        """Readiness: verify connectivity without reading or writing form data."""
        try:
            async with application.state.pool.connection() as connection:
                await connection.execute("SELECT 1")
        except DATABASE_ERRORS as exc:
            log_event("health_database_unavailable", error_type=type(exc).__name__)
            raise HTTPException(status_code=503, detail="Database unavailable") from None
        return {"status": "ok"}

    @application.post(
        "/webhooks/wordpress/forms", response_model=SubmissionResult, status_code=201,
        responses={200: {"model": SubmissionResult, "description": "Previously stored event"}},
        openapi_extra={"requestBody": {"required": True, "content": {
            "application/json": {"schema": FormSubmission.model_json_schema()}
        }}},
    )
    async def receive_form(request: Request, response: Response) -> SubmissionResult:
        configuration = application.state.settings
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > configuration.max_request_bytes:
                raise HTTPException(status_code=413, detail="Request body too large")
            body.extend(chunk)

        try:
            verify_signature(
                bytes(body), request.headers.get("X-Webhook-Timestamp"),
                request.headers.get("X-Webhook-Signature"),
                configuration.webhook_secret.get_secret_value(),
                configuration.signature_tolerance_seconds,
            )
        except HTTPException:
            log_event("authentication_failed")
            raise
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415, detail="Content-Type must be application/json")
        try:
            data = parse_json(bytes(body))
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(status_code=400, detail="Invalid UTF-8 JSON body") from None
        try:
            submission = FormSubmission.model_validate(data)
        except (ValidationError, RecursionError):
            # Do not echo user data in validation errors.
            raise HTTPException(status_code=422, detail="Invalid form payload; see README for schema") from None
        try:
            result = await insert_submission(application.state.pool, submission)
        except EventConflict:
            raise HTTPException(status_code=409, detail="Event ID already used with different formData") from None
        except DATABASE_ERRORS as exc:
            log_event("submission_database_unavailable", error_type=type(exc).__name__)
            raise HTTPException(
                status_code=503, detail="Submission could not be confirmed; retry with the same event ID",
                headers={"Retry-After": "5"},
            ) from None
        response.status_code = 200 if result.duplicate else 201
        log_event("submission_replayed" if result.duplicate else "submission_created", id=str(result.id))
        return result

    return application


app = create_app()
