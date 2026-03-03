import os
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class TaskConfig(BaseModel):
    name: str
    channel_id: str | int
    topic_id: int | None = None
    schedule: str | None = None
    actions: list[str] = ["summarize", "notify"]


class Settings(BaseSettings):
    tg_api_id: int = Field(alias="TG_API_ID")
    tg_api_hash: str = Field(alias="TG_API_HASH")
    gemini_api_key: str = Field(alias="GEMINI_API_KEY")
    groq_api_key: str | None = Field(None, alias="GROQ_API_KEY")
    telegram_bot_token: str | None = Field(None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(None, alias="TELEGRAM_CHAT_ID")
    tg_notify_target: str | None = Field(None, alias="TG_NOTIFY_TARGET")
    preferred_provider: str = "groq"
    summarizer_model: str | list[str] | None = Field(None, alias="SUMMARIZER_MODEL")
    verifier_model: str | list[str] | None = Field(None, alias="VERIFIER_MODEL")
    phone_number: str | None = Field(None, alias="PHONE_NUMBER")
    login_code: str | None = None
    session_path: str = "telebot.session"

    # Global YAML overrides
    lookback_days: int = 1
    report_format: str = "pdf"
    timezone: str = "UTC"
    window_mode: str = "rolling"  # Options: rolling, fixed

    tasks: list[dict[str, Any]] = []

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "populate_by_name": True,
    }


def load_settings(config_path: str = "config.yaml") -> Settings:
    settings = Settings()

    if os.path.exists(config_path):
        with open(config_path) as f:
            config_data = yaml.safe_load(f) or {}

            # Load global overrides
            global_cfg = config_data.get("global", {})
            for key, value in global_cfg.items():
                if hasattr(settings, key):
                    setattr(settings, key, value)

            # Load tasks
            settings.tasks = config_data.get("tasks", [])

    # Post-process models to support comma-separated strings
    if isinstance(settings.summarizer_model, str):
        settings.summarizer_model = [m.strip() for m in settings.summarizer_model.split(",")]
    if isinstance(settings.verifier_model, str):
        settings.verifier_model = [m.strip() for m in settings.verifier_model.split(",")]

    return settings
