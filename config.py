"""Configuration; Cloud Run injects Secret Manager values as environment variables."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from security import decode_webhook_secret


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    webhook_secret: SecretStr = Field(min_length=512, max_length=512)
    db_host: str = Field(min_length=1)
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password: SecretStr = Field(min_length=1)
    db_sslmode: str = Field(default="require", pattern="^(disable|require|verify-ca|verify-full)$")
    db_pool_max_size: int = Field(default=5, ge=1, le=20)
    db_pool_timeout_seconds: int = Field(default=5, ge=1, le=30)
    db_statement_timeout_ms: int = Field(default=10000, ge=1, le=20000)
    signature_tolerance_seconds: int = Field(default=300, ge=1, le=900)
    max_request_bytes: int = Field(default=1048576, ge=1, le=10485760)

    @field_validator("webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value: SecretStr) -> SecretStr:
        decode_webhook_secret(value.get_secret_value())
        return value
