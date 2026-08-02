import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str
    database_url: str
    admin_id: int


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    admin_id_raw = os.getenv("ADMIN_ID", "0").strip()

    if not bot_token:
        raise RuntimeError("Переменная BOT_TOKEN не задана")

    if not database_url:
        raise RuntimeError("Переменная DATABASE_URL не задана")

    try:
        admin_id = int(admin_id_raw)
    except ValueError as error:
        raise RuntimeError("ADMIN_ID должен быть числом") from error

    return Config(
        bot_token=bot_token,
        database_url=database_url,
        admin_id=admin_id,
    )


config = load_config()
