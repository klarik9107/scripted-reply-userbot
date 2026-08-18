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
import os
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
    """One message within a turn: plain text, a media file with an
    optional caption, or a silent notification. Exactly one of
    `text`/`media`/`notify` combinations below is meaningful: text-only
    messages have media=None; media messages may carry `text` as the
    caption sent alongside the file; `notify` entries send their text to
    the notifier account's target instead of the scripted chat."""

    text: Optional[str] = None
    media: Optional[Path] = None
    notify: Optional[str] = None


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
    a plain string (text), an object {"media": "media/photo1.jpg",
    "caption": "optional text"}, or an object {"notify": "text"} - a
    silent off-camera notification sent via the notifier account instead
    of the scripted chat.
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
            elif isinstance(raw_msg, dict) and raw_msg.get("notify"):
                turn.append(ScriptMessage(notify=str(raw_msg["notify"])))
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
    notify_client: Optional[TelegramClient] = None,
    notify_target=None,
) -> None:
    """Show the appropriate chat action, then send one text or media message
    - or, for a `notify` entry, silently deliver it via the notifier
    account instead of showing anything in the scripted chat."""
    if message.notify is not None:
        if notify_client is None or notify_target is None:
            logger.warning(
                "Script has a '!notify' entry but no notifier account is configured "
                "(set NOTIFY_TARGET / log in the notifier session); skipping: %r",
                message.notify,
            )
            return
        await notify_client.send_message(notify_target, message.notify)
        logger.info("Sent off-camera notification: %r", message.notify)
        return

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
        notify_client: Optional[TelegramClient] = None,
        notify_target=None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.script_name = script_name
        self.turns = turns
        self.state_store = state_store
        self.notify_client = notify_client
        self.notify_target = notify_target
        self._lock = asyncio.Lock()

    def _load_state(self) -> ScriptState:
        return self.state_store.load(self.cfg.session_name, self.script_name)

    def _save_state(self, state: ScriptState) -> None:
        self.state_store.save(self.cfg.session_name, self.script_name, state)

    async def load_script(self, script_name: str, turns: List[List[ScriptMessage]]) -> None:
        """Hot-swap the active script and reset progress to turn 0, so a
        newly (re)written scenario is ready to play from the start on the
        very next incoming message - no restart needed."""
        async with self._lock:
            self.script_name = script_name
            self.turns = turns
            self.state_store.reset(self.cfg.session_name, script_name)

    async def handle_incoming(self, event: events.NewMessage.Event) -> None:
        # Use the input entity straight from the event, which carries the
        # access_hash Telethon needs to send a reply. A bare numeric chat_id
        # isn't enough if this session hasn't independently cached that
        # entity (e.g. target configured as a numeric id with no prior
        # dialog/contact history).
        chat_id = await event.get_input_chat()
        log_chat_id = event.chat_id
        text = (event.raw_text or "").strip()

        if text == RESET_COMMAND:
            self.reset()
            logger.info("Received reset command from target in chat %s; progress reset to turn 0", log_chat_id)
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
                    log_chat_id,
                )
                return

            await self._play_turn(chat_id, state, log_chat_id)

    async def _play_turn(self, chat_id, state: ScriptState, log_chat_id: int) -> None:
        turn = self.turns[state.turn_index]
        resuming_mid_turn = state.message_index > 0

        logger.info(
            "Playing turn %d/%d (from message %d/%d) for script %r in chat %s",
            state.turn_index + 1,
            len(self.turns),
            state.message_index + 1,
            len(turn),
            self.script_name,
            log_chat_id,
        )

        if not resuming_mid_turn:
            # Simulate "read then think" delay before starting to reply.
            # Skipped when resuming mid-turn after a crash - the target
            # already saw a partial reply, no need to re-simulate reading.
            read_delay = random.uniform(self.cfg.read_delay_min, self.cfg.read_delay_max)
            logger.debug("Waiting %.2fs to simulate reading", read_delay)
            await asyncio.sleep(read_delay)

        for i in range(state.message_index, len(turn)):
            await send_single_message(
                self.client,
                chat_id,
                turn[i],
                self.cfg,
                notify_client=self.notify_client,
                notify_target=self.notify_target,
            )
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
# Live script reloading from the authoring chat
# --------------------------------------------------------------------------

AUTHORING_FETCH_LIMIT = 2000


async def _reload_active_script_from_authoring_chat(
    client: TelegramClient,
    cfg: Config,
    player: "ScriptPlayer",
    authoring_entity: str,
    extra_author_ids: set,
) -> None:
    """Re-parse the authoring chat and, if it defines any '# name' scenario,
    make the most recently written one the active script - so the moment
    you finish sending a scenario there, the bot is ready to play it back
    on the very next message from the target."""
    scripts = await fetch_scripts_from_chat(
        client, cfg, authoring_entity, AUTHORING_FETCH_LIMIT, extra_author_ids
    )
    if not scripts:
        return

    name = next(reversed(scripts))
    turns_raw = scripts[name]
    if not turns_raw:
        logger.warning("Latest scenario %r in authoring chat has no turns yet; not switching.", name)
        return

    out_path = cfg.scripts_dir / f"{name}.json"
    out_path.write_text(
        json.dumps({"turns": turns_raw}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    turns = load_script(out_path)  # validate + convert to ScriptMessage objects

    await player.load_script(name, turns)
    logger.info(
        "Live-switched to scenario %r (%d turns) from authoring chat, ready from turn 0",
        name,
        len(turns),
    )


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

    notify_client: Optional[TelegramClient] = None
    notify_target = None
    if cfg.notify_target:
        notify_client = TelegramClient(cfg.notify_session_name, cfg.api_id, cfg.api_hash)
        await notify_client.connect()
        if not await notify_client.is_user_authorized():
            logger.error(
                "Notifier session %r is not authorized. Log in with it once "
                "(e.g. temporarily set SESSION_NAME=%s and run 'run') before "
                "using '!notify' in scripts. Notifications are disabled for now.",
                cfg.notify_session_name,
                cfg.notify_session_name,
            )
            await notify_client.disconnect()
            notify_client = None
        else:
            notify_target_raw = cfg.notify_target.strip()
            notify_target = await notify_client.get_entity(
                notify_target_raw if notify_target_raw.startswith("@") else f"@{notify_target_raw}"
            )
            logger.info("Notifier account ready; '!notify' lines will be sent to %r", cfg.notify_target)

    player = ScriptPlayer(client, cfg, script_name, turns, state_store, notify_client, notify_target)

    @client.on(events.NewMessage(incoming=True))
    async def _handler(event: events.NewMessage.Event) -> None:  # noqa: ANN401
        sender_id = event.sender_id
        if sender_id != target_id:
            return
        try:
            await player.handle_incoming(event)
        except Exception:  # noqa: BLE001 - keep the bot alive on per-message errors
            logger.exception("Error while handling incoming message from target")

    if cfg.authoring_chat:
        authoring_entity = (
            "me" if cfg.authoring_chat.strip().lower() == "me" else cfg.authoring_chat.strip()
        )
        authoring_extra_author_ids = await resolve_authoring_author_ids(client, cfg)

        @client.on(events.NewMessage(chats=authoring_entity))
        async def _authoring_handler(event: events.NewMessage.Event) -> None:  # noqa: ANN401
            if not (event.out or event.sender_id in authoring_extra_author_ids):
                return  # ignore anyone else who might post in that chat
            try:
                await _reload_active_script_from_authoring_chat(
                    client, cfg, player, authoring_entity, authoring_extra_author_ids
                )
            except Exception:  # noqa: BLE001 - keep the bot alive on per-message errors
                logger.exception("Error while reloading script from authoring chat")

        logger.info(
            "Watching authoring chat %r (co-authors: %s) - the latest '# name' scenario "
            "written there goes live immediately, no restart needed.",
            cfg.authoring_chat,
            ", ".join(cfg.authoring_extra_authors) or "none",
        )

    logger.info(
        "scripted-reply-userbot is running. script=%r session=%r target=%r. Press Ctrl+C to stop.",
        script_name,
        cfg.session_name,
        cfg.target,
    )

    retries_left = cfg.reconnect_retries
    try:
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
    finally:
        if notify_client is not None:
            await notify_client.disconnect()


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


SCRIPT_HEADER_PREFIX = "#"
TURN_DELIMITER = "-"
NOTIFY_PREFIX = "!notify"


def _slugify_script_name(raw: str) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in raw.strip().lower())
    slug = "_".join(filter(None, slug.split("_")))
    return slug or "untitled"


@cli.command("export-script")
@click.option(
    "--from",
    "from_chat",
    required=True,
    help="Chat to export from: @username, numeric id, or 'me' for Saved Messages",
)
@click.option(
    "--name",
    "script_name",
    default=None,
    help="Only export the section headed '# <name>'. Omit to export every "
    "'# ...' section found in the chat.",
)
@click.option("--limit", default=1000, show_default=True, help="Max number of messages to scan")
def export_script_command(from_chat: str, script_name: Optional[str], limit: int) -> None:
    """Turn messages you wrote in a Telegram chat into one or more script JSON files.

    Write your scenario(s) as plain messages in a chat (e.g. a private
    channel or group with just yourself): start each scenario with a line
    '# scenario_name', separate turns with a line that is just '-'.
    Handy for writing scenarios from your phone - run this once from a PC
    to generate scripts/<name>.json for each section found.
    """
    try:
        cfg = load_config()
    except ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)

    try:
        asyncio.run(_export_script(cfg, from_chat, script_name, limit))
    except AuthKeyUnregisteredError:
        logger.error(
            "The saved session is no longer valid (revoked or logged out elsewhere). "
            "Delete the .session file and run 'run' once to log in interactively."
        )
        sys.exit(1)


async def resolve_authoring_author_ids(client: TelegramClient, cfg: Config) -> set:
    """Resolve AUTHORING_EXTRA_AUTHORS usernames/ids into user ids, so
    scenario messages written by co-authors (not just yourself) are picked
    up from the authoring chat."""
    ids = set()
    for raw in cfg.authoring_extra_authors:
        raw = raw.strip()
        try:
            if raw.lstrip("-").isdigit():
                ids.add(int(raw))
            else:
                entity = await client.get_entity(raw if raw.startswith("@") else f"@{raw}")
                ids.add(entity.id)
        except Exception:  # noqa: BLE001 - a bad/unreachable username shouldn't break startup
            logger.warning("Could not resolve authoring co-author %r; ignoring.", raw)
    return ids


async def fetch_scripts_from_chat(
    client: TelegramClient,
    cfg: Config,
    from_chat: str,
    limit: int,
    extra_author_ids: Optional[set] = None,
) -> "dict[str, List[List[dict]]]":
    """Fetch and parse every '# name' scenario section out of a chat's
    message history. Shared by the one-shot 'export-script' command and the
    live authoring-chat watcher in 'run'.
    """
    extra_author_ids = extra_author_ids or set()
    entity = "me" if from_chat.strip().lower() == "me" else from_chat.strip()
    messages = [
        msg
        async for msg in client.iter_messages(entity, limit=limit, reverse=True)
        if (msg.out or msg.sender_id in extra_author_ids) and (msg.text or msg.media)
    ]
    return await _parse_scripts_from_messages(client, cfg, messages)


async def _parse_scripts_from_messages(
    client: TelegramClient, cfg: Config, messages: list
) -> "dict[str, List[List[dict]]]":
    # A single Telegram message may itself contain multiple newline-separated
    # lines (very common when typing on a phone: each Enter starts a new
    # line, and the whole block is sent as one message). Flatten every
    # message into a stream of tokens - one per line for text, one for each
    # media message - so '# name' headers and '-' delimiters are recognised
    # regardless of whether they arrived as separate messages or as lines
    # within one message.
    tokens: List[tuple] = []
    for msg in messages:
        if msg.media:
            caption = (msg.text or "").strip() or None
            tokens.append(("media", msg, caption))
        else:
            for line in (msg.text or "").splitlines():
                line = line.strip()
                if line:
                    tokens.append(("text", line))

    scripts: "dict[str, List[List[dict]]]" = {}
    current_name: Optional[str] = None

    for token in tokens:
        kind = token[0]
        text = token[1] if kind == "text" else None

        if kind == "text" and text.startswith(SCRIPT_HEADER_PREFIX):
            current_name = _slugify_script_name(text[len(SCRIPT_HEADER_PREFIX):])
            scripts[current_name] = [[]]
            continue

        if current_name is None:
            continue  # ignore stray content before the first '# name' header

        turns = scripts[current_name]

        if kind == "text" and text == TURN_DELIMITER:
            if turns[-1]:
                turns.append([])
            continue

        if kind == "text" and text.startswith(NOTIFY_PREFIX):
            if len(turns[-1]) >= 3:
                turns.append([])
            notify_text = text[len(NOTIFY_PREFIX):].strip()
            if notify_text:
                turns[-1].append({"notify": notify_text})
            continue

        if len(turns[-1]) >= 3:
            turns.append([])

        if kind == "media":
            _, msg, caption = token
            media_dir = cfg.scripts_dir / "media" / current_name
            media_dir.mkdir(parents=True, exist_ok=True)
            saved_path = await _download_message_media(client, msg, media_dir)
            entry: dict = {"media": f"media/{current_name}/{saved_path.name}"}
            if caption:
                entry["caption"] = caption
            turns[-1].append(entry)
        else:
            turns[-1].append(text)

    # Drop any trailing empty turn left over from a dangling '-'.
    for turns in scripts.values():
        if turns and not turns[-1]:
            turns.pop()

    return scripts


async def _export_script(
    cfg: Config, from_chat: str, script_name: Optional[str], limit: int
) -> None:
    client = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ConfigError(
            "No authorized session found. Run 'python bot.py run --script <name>' once "
            "first to log in interactively."
        )

    extra_author_ids = await resolve_authoring_author_ids(client, cfg)
    scripts = await fetch_scripts_from_chat(client, cfg, from_chat, limit, extra_author_ids)
    await client.disconnect()

    if not scripts:
        logger.error(
            "No '# name' sections found in %r. Start your scenario with a "
            "message like '# demo'.",
            from_chat,
        )
        sys.exit(1)

    if script_name is not None:
        wanted = _slugify_script_name(script_name)
        if wanted not in scripts:
            logger.error(
                "No section '# %s' found in %r. Sections found: %s",
                script_name,
                from_chat,
                ", ".join(scripts) or "(none)",
            )
            sys.exit(1)
        scripts = {wanted: scripts[wanted]}

    for name, turns in scripts.items():
        if not turns:
            logger.warning("Section %r has no turns; skipping.", name)
            continue
        out_path = cfg.scripts_dir / f"{name}.json"
        out_path.write_text(
            json.dumps({"turns": turns}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        load_script(out_path)  # validate against the same loader the bot uses
        total_messages = sum(len(t) for t in turns)
        click.echo(f"Exported {len(turns)} turns ({total_messages} messages) to {out_path}")


async def _download_message_media(client: TelegramClient, msg, media_dir: Path) -> Path:
    saved = await client.download_media(msg, file=str(media_dir) + os.sep)
    return Path(saved)


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
