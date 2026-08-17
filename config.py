"""Configuration loading for scripted-reply-userbot.

All secrets and per-deployment settings come from a `.env` file
(see `.env.example`). Nothing here should ever be hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root regardless of current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str
    target: str  # username (with or without @) or numeric user id, as string

    # Typing / pacing behaviour (all overridable via .env).
    read_delay_min: float
    read_delay_max: float
    typing_chars_per_second: float
    typing_delay_min: float
    typing_delay_max: float
    inter_message_delay_min: float
    inter_message_delay_max: float

    # Telethon reconnection behaviour.
    reconnect_retries: int
    reconnect_delay: float

    scripts_dir: Path
    state_dir: Path


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name}={raw!r} is not a valid number") from exc


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name}={raw!r} is not a valid integer") from exc


def load_config() -> Config:
    """Read and validate configuration from environment variables (.env)."""

    api_id_raw = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    session_name = os.getenv("SESSION_NAME", "userbot_session")
    target = os.getenv("TARGET_USERNAME_OR_ID")

    missing = [
        name
        for name, value in (
            ("API_ID", api_id_raw),
            ("API_HASH", api_hash),
            ("TARGET_USERNAME_OR_ID", target),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "Missing required configuration: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in the values "
            "(get API_ID / API_HASH from https://my.telegram.org)."
        )

    try:
        api_id = int(api_id_raw)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ConfigError(f"API_ID must be an integer, got {api_id_raw!r}") from exc

    return Config(
        api_id=api_id,
        api_hash=api_hash,  # type: ignore[arg-type]
        session_name=session_name,
        target=target,  # type: ignore[arg-type]
        read_delay_min=_get_float("READ_DELAY_MIN", 1.5),
        read_delay_max=_get_float("READ_DELAY_MAX", 4.0),
        typing_chars_per_second=_get_float("TYPING_CHARS_PER_SECOND", 6.0),
        typing_delay_min=_get_float("TYPING_DELAY_MIN", 1.0),
        typing_delay_max=_get_float("TYPING_DELAY_MAX", 6.0),
        inter_message_delay_min=_get_float("INTER_MESSAGE_DELAY_MIN", 0.8),
        inter_message_delay_max=_get_float("INTER_MESSAGE_DELAY_MAX", 2.5),
        reconnect_retries=_get_int("RECONNECT_RETRIES", 10),
        reconnect_delay=_get_float("RECONNECT_DELAY", 5.0),
        scripts_dir=PROJECT_ROOT / "scripts",
        state_dir=PROJECT_ROOT / ".state",
    )
