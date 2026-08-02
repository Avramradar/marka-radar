from functools import lru_cache

from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Config(BaseSettings):
    bot_token: str = Field(alias="BOT_TOKEN")
    database_url: str = Field(alias="DATABASE_URL")
    admin_id: int = Field(alias="ADMIN_ID")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @field_validator("bot_token")
    @classmethod
    def validate_bot_token(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("Переменная BOT_TOKEN не задана")

        if ":" not in value:
            raise ValueError(
                "BOT_TOKEN имеет неправильный формат"
            )

        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("Переменная DATABASE_URL не задана")

        required_prefix = "postgresql+asyncpg://"

        if not value.startswith(required_prefix):
            raise ValueError(
                "DATABASE_URL должен начинаться с "
                "postgresql+asyncpg://"
            )

        return value

    @field_validator("admin_id")
    @classmethod
    def validate_admin_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                "ADMIN_ID должен быть положительным числом"
            )

        return value


@lru_cache
def load_config() -> Config:
    return Config()


config = load_config()
