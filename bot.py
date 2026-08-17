#!/usr/bin/env python3
"""scripted-reply-userbot: plays back a scripted conversation on Telegram.

A Telethon USERBOT (runs as your own account, not the Bot API) that
watches for incoming messages (text or media) from a specific target
user - in any chat they message from, private or group - and replies
with pre-written "turns" from a JSON script, one turn per incoming
message, with simulated read-delay and typing/upload indicators.

For demonstration / staged-video use only. Only run this against your
own secondary Telegram accounts. See README.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import click
from telethon import TelegramClient, events
from telethon.errors import (
    ApiIdInvalidError,
    AuthKeyUnregisteredError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User

from config import PROJECT_ROOT, Config, ConfigError, load_config
from state_store import ScriptState, StateStore

RESET_COMMAND = "/reset_script"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scripted_reply_userbot")


# --------------------------------------------------------------------------
# Script loading
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScriptMessage:
    """One message within a turn: plain text, or a media file with an
    optional caption. Exactly one of `text`/`media` combinations below is
    meaningful: text-only messages have media=None; media messages may
    carry `text` as the caption sent alongside the file."""

    text: Optional[str] = None
    media: Optional[Path] = None


# Media file extensions mapped to the "chat action" Telegram shows while
# the other side is preparing that kind of attachment (typing indicators
# are type-specific, e.g. "uploading photo" rather than "typing").
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_AUDIO_EXTS = {".ogg", ".oga", ".mp3", ".wav", ".m4a"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".webm", ".mkv"}


def _media_action_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _IMAGE_EXTS:
        return "upload-photo"
    if ext in _AUDIO_EXTS:
        return "upload-audio"
    if ext in _VIDEO_EXTS:
        return "upload-video"
    return "upload-document"


def _resolve_media_path(raw: str, script_path: Path) -> Path:
    """Resolve a media path from a script: absolute as-is, otherwise
    relative to the script file's own directory, falling back to the
    project root."""
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate

    near_script = script_path.parent / candidate
    if near_script.exists():
        return near_script

    return PROJECT_ROOT / candidate


def load_script(path: Path) -> List[List[ScriptMessage]]:
    """Load and validate a conversation script from JSON.

    Expected format:
        {"turns": [["msg1", "msg2"], ["msg3"], ...]}
    Each turn is a list of 1-3 messages sent back to back. A message is
    either a plain string (text) or an object:
        {"media": "media/photo1.jpg", "caption": "optional text"}
    """
    if not path.exists():
        raise FileNotFoundError(f"Script file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    raw_turns = data.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ValueError(f"Script {path} must contain a non-empty 'turns' list")

    turns: List[List[ScriptMessage]] = []
    for i, raw_turn in enumerate(raw_turns):
        if not isinstance(raw_turn, list) or not (1 <= len(raw_turn) <= 3):
            raise ValueError(
                f"Script {path}: turn #{i} must be a list of 1-3 messages, got {raw_turn!r}"
            )

        turn: List[ScriptMessage] = []
        for raw_msg in raw_turn:
            if isinstance(raw_msg, str):
                if not raw_msg.strip():
                    raise ValueError(f"Script {path}: turn #{i} contains an empty text message")
                turn.append(ScriptMessage(text=raw_msg))
            elif isinstance(raw_msg, dict) and raw_msg.get("media"):
                media_path = _resolve_media_path(str(raw_msg["media"]), path)
                if not media_path.exists():
                    raise ValueError(
                        f"Script {path}: turn #{i} references missing media file {media_path}"
                    )
                caption = raw_msg.get("caption")
                if caption is not None and not isinstance(caption, str):
                    raise ValueError(f"Script {path}: turn #{i} has a non-string caption")
                turn.append(ScriptMessage(text=caption, media=media_path))
            else:
                raise ValueError(
                    f"Script {path}: turn #{i} contains an invalid message {raw_msg!r} "
                    "(must be a string, or an object with a 'media' key)"
                )
        turns.append(turn)

    return turns


def list_available_scripts(scripts_dir: Path) -> List[Path]:
    if not scripts_dir.exists():
        return []
    return sorted(scripts_dir.glob("*.json"))


# --------------------------------------------------------------------------
# Typing-simulation helpers
# --------------------------------------------------------------------------

def compute_typing_duration(text: str, cfg: Config) -> float:
    """Typing time proportional to message length, clamped to sane bounds."""
    raw = len(text) / cfg.typing_chars_per_second
    return max(cfg.typing_delay_min, min(raw, cfg.typing_delay_max))


def compute_action_duration(message: ScriptMessage, cfg: Config) -> float:
    """Duration of the pre-send chat action (typing / uploading photo /
    etc). Proportional to caption/text length when there is text, otherwise
    a random duration within the same configured bounds."""
    if message.text:
        return compute_typing_duration(message.text, cfg)
    return random.uniform(cfg.typing_delay_min, cfg.typing_delay_max)


async def send_single_message(
    client: TelegramClient,
    chat_id: int,
    message: ScriptMessage,
    cfg: Config,
) -> None:
    """Show the appropriate chat action, then send one text or media message."""
    duration = compute_action_duration(message, cfg)

    if message.media is not None:
        action_type = _media_action_type(message.media)
        logger.debug(
            "Chat action %r for %.2fs before sending media: %s", action_type, duration, message.media
        )
        async with client.action(chat_id, action_type):
            await asyncio.sleep(duration)
        await client.send_file(chat_id, str(message.media), caption=message.text)
        logger.info("Sent media to chat %s: %s (caption=%r)", chat_id, message.media, message.text)
    else:
        logger.debug("Typing indicator for %.2fs before sending: %r", duration, message.text)
        async with client.action(chat_id, "typing"):
            await asyncio.sleep(duration)
        await client.send_message(chat_id, message.text)
        logger.info("Sent message to chat %s: %r", chat_id, message.text)


# --------------------------------------------------------------------------
# Core playback logic
# --------------------------------------------------------------------------

class ScriptPlayer:
    """Owns the current progress for one (session, script) pair and reacts
    to incoming messages from the target user, in whichever chat (private
    or group) they send them.

    Incoming messages are handled strictly one at a time (the client is
    started with sequential_updates=True, backed by this lock as a second
    line of defense), so if the target fires off several messages while a
    turn is still being sent, later ones simply wait their turn instead of
    being dropped or processed out of order.
    """

    def __init__(
        self,
        client: TelegramClient,
        cfg: Config,
        script_name: str,
        turns: List[List[ScriptMessage]],
        state_store: StateStore,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.script_name = script_name
        self.turns = turns
        self.state_store = state_store
        self._lock = asyncio.Lock()

    def _load_state(self) -> ScriptState:
        return self.state_store.load(self.cfg.session_name, self.script_name)

    def _save_state(self, state: ScriptState) -> None:
        self.state_store.save(self.cfg.session_name, self.script_name, state)

    async def handle_incoming(self, event: events.NewMessage.Event) -> None:
        chat_id = event.chat_id
        text = (event.raw_text or "").strip()

        if text == RESET_COMMAND:
            self.reset()
            logger.info("Received reset command from target in chat %s; progress reset to turn 0", chat_id)
            # Silently acknowledge with a reaction instead of a visible reply,
            # so the mechanic isn't revealed in the chat.
            try:
                await event.message.mark_read()
            except Exception:  # noqa: BLE001 - best-effort, never fatal
                logger.debug("Could not mark reset message as read", exc_info=True)
            return

        async with self._lock:
            state = self._load_state()
            if state.turn_index >= len(self.turns):
                logger.info(
                    "Script %r already finished (turn %d/%d); staying silent in chat %s",
                    self.script_name,
                    state.turn_index,
                    len(self.turns),
                    chat_id,
                )
                return

            await self._play_turn(chat_id, state)

    async def _play_turn(self, chat_id: int, state: ScriptState) -> None:
        turn = self.turns[state.turn_index]
        resuming_mid_turn = state.message_index > 0

        logger.info(
            "Playing turn %d/%d (from message %d/%d) for script %r in chat %s",
            state.turn_index + 1,
            len(self.turns),
            state.message_index + 1,
            len(turn),
            self.script_name,
            chat_id,
        )

        if not resuming_mid_turn:
            # Simulate "read then think" delay before starting to reply.
            # Skipped when resuming mid-turn after a crash - the target
            # already saw a partial reply, no need to re-simulate reading.
            read_delay = random.uniform(self.cfg.read_delay_min, self.cfg.read_delay_max)
            logger.debug("Waiting %.2fs to simulate reading", read_delay)
            await asyncio.sleep(read_delay)

        for i in range(state.message_index, len(turn)):
            await send_single_message(self.client, chat_id, turn[i], self.cfg)
            # Save after every individual message so a crash mid-turn
            # resumes from the next unsent message, not from scratch.
            self._save_state(ScriptState(turn_index=state.turn_index, message_index=i + 1))

            if i < len(turn) - 1:
                gap = random.uniform(self.cfg.inter_message_delay_min, self.cfg.inter_message_delay_max)
                await asyncio.sleep(gap)

        self._save_state(ScriptState(turn_index=state.turn_index + 1, message_index=0))

    def reset(self) -> None:
        self.state_store.reset(self.cfg.session_name, self.script_name)


# --------------------------------------------------------------------------
# Target resolution
# --------------------------------------------------------------------------

async def resolve_target_id(client: TelegramClient, target: str) -> int:
    """Resolve a username (with/without @) or numeric id string to a user id."""
    target = target.strip()
    if target.lstrip("-").isdigit():
        return int(target)

    entity = await client.get_entity(target if target.startswith("@") else f"@{target}")
    if not isinstance(entity, User):
        raise ConfigError(f"TARGET_USERNAME_OR_ID={target!r} does not resolve to a user")
    return entity.id


# --------------------------------------------------------------------------
# Connection / run loop with reconnection handling
# --------------------------------------------------------------------------

async def run_bot(cfg: Config, script_path: Path) -> None:
    script_name = script_path.stem
    turns = load_script(script_path)
    state_store = StateStore(cfg.state_dir)

    # sequential_updates=True makes Telethon await each event handler fully
    # before dispatching the next update, so incoming messages from the
    # target are processed strictly in order (queued) instead of
    # concurrently while a turn is still being sent.
    client = TelegramClient(
        cfg.session_name, cfg.api_id, cfg.api_hash, sequential_updates=True
    )

    try:
        await client.connect()
    except ApiIdInvalidError as exc:
        raise ConfigError(
            "Telegram rejected API_ID/API_HASH as invalid. Double-check the values "
            "from https://my.telegram.org against your .env file."
        ) from exc

    if not await client.is_user_authorized():
        session_file = Path(f"{cfg.session_name}.session")
        if session_file.exists():
            raise ConfigError(
                f"Session file {session_file} exists but is not authorized "
                "(it may have been revoked). Delete it and re-run this command "
                "to log in again interactively."
            )
        logger.info(
            "No existing session found. Starting one-time interactive login "
            "(you will be asked for your phone number and the code Telegram sends you)."
        )
        try:
            await client.start()
        except SessionPasswordNeededError:
            logger.error(
                "This account has two-step verification enabled. Re-run and "
                "enter the cloud password when prompted."
            )
            raise
        except PhoneNumberInvalidError as exc:
            raise ConfigError("The phone number entered was rejected by Telegram as invalid.") from exc

    target_id = await resolve_target_id(client, cfg.target)
    logger.info("Resolved target %r to user id %s", cfg.target, target_id)

    player = ScriptPlayer(client, cfg, script_name, turns, state_store)

    @client.on(events.NewMessage(incoming=True))
    async def _handler(event: events.NewMessage.Event) -> None:  # noqa: ANN401
        sender_id = event.sender_id
        if sender_id != target_id:
            return
        try:
            await player.handle_incoming(event)
        except Exception:  # noqa: BLE001 - keep the bot alive on per-message errors
            logger.exception("Error while handling incoming message from target")

    logger.info(
        "scripted-reply-userbot is running. script=%r session=%r target=%r. Press Ctrl+C to stop.",
        script_name,
        cfg.session_name,
        cfg.target,
    )

    retries_left = cfg.reconnect_retries
    while True:
        try:
            await client.run_until_disconnected()
            break  # clean disconnect (e.g. log_out), stop the loop
        except (ConnectionError, OSError) as exc:
            if retries_left <= 0:
                logger.error("Exhausted reconnect attempts; giving up. Last error: %s", exc)
                raise
            retries_left -= 1
            logger.warning(
                "Connection lost (%s). Reconnecting in %.1fs (%d attempts left)...",
                exc,
                cfg.reconnect_delay,
                retries_left,
            )
            await asyncio.sleep(cfg.reconnect_delay)
            await client.connect()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """scripted-reply-userbot — plays back a scripted Telegram conversation."""


@cli.command("run")
@click.option(
    "--script",
    "script_arg",
    required=True,
    help="Path to a script JSON file, e.g. scripts/demo.json",
)
def run_command(script_arg: str) -> None:
    """Start listening and playing back the given script."""
    try:
        cfg = load_config()
        script_path = _resolve_script_path(cfg, script_arg)
        asyncio.run(run_bot(cfg, script_path))
    except ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Invalid script: %s", exc)
        sys.exit(1)
    except AuthKeyUnregisteredError:
        logger.error(
            "The saved session is no longer valid (revoked or logged out elsewhere). "
            "Delete the .session file and run again to log in interactively."
        )
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")


@cli.command("list-scripts")
def list_scripts_command() -> None:
    """Show available script files in the scripts/ directory."""
    try:
        cfg = load_config()
    except ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)

    scripts = list_available_scripts(cfg.scripts_dir)
    if not scripts:
        click.echo(f"No scripts found in {cfg.scripts_dir}")
        return

    for path in scripts:
        try:
            turns = load_script(path)
            click.echo(f"{path.stem:20s} ({len(turns)} turns) - {path}")
        except (ValueError, json.JSONDecodeError) as exc:
            click.echo(f"{path.stem:20s} INVALID: {exc}")


@cli.command("reset")
@click.option("--script", "script_arg", required=True, help="Script name (without .json) or path")
def reset_command(script_arg: str) -> None:
    """Reset playback progress for a script without touching Telegram."""
    try:
        cfg = load_config()
        script_path = _resolve_script_path(cfg, script_arg)
    except ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)

    state_store = StateStore(cfg.state_dir)
    state_store.reset(cfg.session_name, script_path.stem)
    click.echo(f"Progress reset for script {script_path.stem!r} (session {cfg.session_name!r}).")


def _resolve_script_path(cfg: Config, script_arg: str) -> Path:
    """Accept either a bare name ('demo') or a path ('scripts/demo.json')."""
    candidate = Path(script_arg)
    if candidate.suffix == ".json" and candidate.exists():
        return candidate

    by_name = cfg.scripts_dir / f"{script_arg}.json"
    if by_name.exists():
        return by_name

    if candidate.exists():
        return candidate

    raise FileNotFoundError(
        f"Could not find script {script_arg!r} (looked for {candidate} and {by_name})"
    )


if __name__ == "__main__":
    cli()
