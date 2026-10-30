"""The envelope is strict; formData retains arbitrary JSON fields and nesting."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue, StringConstraints, field_validator

Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]


class FormSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    formId: Identifier
    postId: Identifier | None = None
    formData: dict[str, JsonValue]

    @field_validator("formId", "postId")
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("Identifier must be nonblank and contain no null characters")
        if value is not None:
            value.encode("utf-8")
        return value

    @field_validator("formData")
    @classmethod
    def validate_data(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        def check(item: JsonValue) -> None:
            if isinstance(item, str) and "\x00" in item:
                raise ValueError("PostgreSQL JSONB cannot store null characters")
            if isinstance(item, str):
                item.encode("utf-8")
            if isinstance(item, list):
                for child in item:
                    check(child)
            if isinstance(item, dict):
                for key, child in item.items():
                    check(key)
                    check(child)

        check(value)
        if "_event_id" in value:
            event_id = value["_event_id"]
            if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 255:
                raise ValueError("_event_id must be a nonblank string of at most 255 characters")
        return value


class SubmissionResult(BaseModel):
    id: UUID
    created_at: datetime
    duplicate: bool = False
