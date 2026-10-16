"""Settings from the environment (or a .env file next to where you run the bot)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    telegram_token: str | None
    chat_id: int | None
    database_url: str | None
    template_path: Path


def load_settings() -> Settings:
    load_dotenv(os.environ.get("FT_ENV_FILE", ".env"))
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return Settings(
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        chat_id=int(chat) if chat else None,
        database_url=os.environ.get("DATABASE_URL") or None,
        template_path=Path(os.environ.get("FT_TEMPLATE", "config/week_template.yaml")),
    )
