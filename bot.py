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
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import click
from telethon import TelegramClient, events
from telethon.errors import (
    ApiIdInvalidError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    TtlMediaInvalidError,
)
from telethon.sessions import StringSession
from telethon.tl.types import User

from config import PROJECT_ROOT, Config, ConfigError, load_config
import media_wait
from media_wait import BOT_NOTICE_PREFIX, MEDIA_COMMANDS
from state_store import ScriptState, StateStore

RESET_COMMAND = "/reset_script"

# Extra pre-send pause (on top of the normal 1.5-3.0s "read" pause) before
# a '> text' me-reply, when a '!notify' was sent immediately before it in
# the same turn - gives the off-camera push notification time to actually
# land on screen before the "me" client fires back a reply to Maria.
NOTIFY_READ_DELAY = 3.0

# Hard safety cap: a single script run never sends more than this many
# '> text' auto-replies via the "me" client, however many the scenario
# defines - a runaway/looping scenario shouldn't be able to auto-pilot an
# unbounded conversation on a real account.
ME_MAX_REPLIES_PER_SCRIPT = 10

# How long a just-sent me-reply's text stays "armed" for echo suppression.
# Generous on purpose: with sequential_updates=True the echo is only handled
# after the current (possibly multi-turn, multi-minute) auto-chain finishes.
ME_ECHO_TTL_SECONDS = 600.0

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
    the notifier account's target instead of the scripted chat.
    `delete_after` is only meaningful on a plain text message (not media,
    not notify): if set, the sent message is auto-deleted that many
    seconds later, in the background, without blocking playback.
    `wait_before_next` is only meaningful on a visible message (text or
    media, not notify): if set, it replaces the normal randomised
    inter-message gap with this exact pause before the next message in
    the turn is sent. `ttl` is only meaningful on a media message (photo
    or video): if set (1-60 seconds), the file is sent as Telegram
    self-destructing media with that view timer. `is_me` marks a '> text'
    auto-reply sent by the second ("me") Telethon client logged into your
    own account to Maria's chat, instead of a normal scripted message from
    Maria - `text` holds its content and `wait_before_next` still applies,
    but `delete_after`/`ttl` never do."""

    text: Optional[str] = None
    media: Optional[Path] = None
    notify: Optional[str] = None
    delete_after: Optional[float] = None
    wait_before_next: Optional[float] = None
    ttl: Optional[int] = None
    is_me: bool = False


# Media file extensions mapped to the "chat action" Telegram shows while
# the other side is preparing that kind of attachment (typing indicators
# are type-specific, e.g. "uploading photo" rather than "typing").
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_AUDIO_EXTS = {".ogg", ".oga", ".mp3", ".wav", ".m4a"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".webm", ".mkv"}


def _media_action_type(path: Path) -> str:
    # Must match Telethon's _ChatAction._str_mapping keys exactly - it's
    # 'photo'/'audio'/'video'/'document', NOT 'upload-photo' etc. (that
    # prefix doesn't exist and makes client.action() raise ValueError
    # before send_file() ever runs, silently killing media playback).
    ext = path.suffix.lower()
    if ext in _IMAGE_EXTS:
        return "photo"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    return "document"


def _resolve_referenced_media(raw: str, cfg: Config) -> Optional[Path]:
    """Resolve a '!media <path>' reference typed directly into a scenario
    message in the authoring chat, so an author can point at a photo/video/
    voice file they've already prepared on disk instead of having to attach
    it as an actual Telegram message. Tried in order: the path as given (if
    absolute), under scripts/media/, directly under scripts/, then under the
    project root. Returns None if no match exists anywhere."""
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    for root in (cfg.scripts_dir / "media", cfg.scripts_dir, PROJECT_ROOT):
        path = root / candidate
        if path.exists():
            return path
    return None


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


def _is_notify_entry(raw_msg: object) -> bool:
    return isinstance(raw_msg, dict) and bool(raw_msg.get("notify"))


def _is_media_entry(raw_msg: object) -> bool:
    # A '<фото>'/'<видео>' placeholder counts too, so '!timer' can attach to
    # it before the real file has arrived.
    return isinstance(raw_msg, dict) and bool(raw_msg.get("media") or raw_msg.get("placeholder"))


def _is_me_entry(raw_msg: object) -> bool:
    return isinstance(raw_msg, dict) and bool(raw_msg.get("me"))


def _is_visible_entry(raw_msg: object) -> bool:
    """True for anything that counts against the 3-visible-message-per-turn
    cap: Maria's own text/media. '!notify' entries and '> text' ("me")
    auto-replies are both silent/separate-channel and don't count."""
    return not _is_notify_entry(raw_msg) and not _is_me_entry(raw_msg)


_LEADING_NUMBER_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)")


def _leading_number(rest: str, directive: str, scenario: Optional[str]) -> float:
    """Parse the leading number of a directive argument ('!delete 10',
    '!wait 0,5', but also '!delete 10с после показа'). Anything after the
    number is treated as a comment and ignored (with a log note) instead of
    silently dropping the whole directive. Raises ValueError if the argument
    doesn't start with a number."""
    match = _LEADING_NUMBER_RE.match(rest)
    if match is None:
        raise ValueError(f"{directive} argument {rest!r} doesn't start with a number")
    trailing = rest[match.end():].strip()
    if trailing:
        logger.info(
            "Scenario %r: '%s %s' - using the number %s and ignoring the extra text %r.",
            scenario,
            directive,
            rest,
            match.group(1),
            trailing,
        )
    return float(match.group(1).replace(",", "."))


def _parse_optional_positive_float(raw_msg: dict, key: str, path: Path, i: int) -> Optional[float]:
    value = raw_msg.get(key)
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Script {path}: turn #{i} has a non-numeric {key}={value!r}") from exc
    if value <= 0:
        raise ValueError(f"Script {path}: turn #{i} has {key}={value!r}, must be > 0")
    return value


def _parse_optional_ttl(raw_msg: dict, path: Path, i: int) -> Optional[int]:
    value = raw_msg.get("ttl")
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Script {path}: turn #{i} has a non-integer ttl={value!r}") from exc
    if not 1 <= value <= 60:
        raise ValueError(
            f"Script {path}: turn #{i} has ttl={value!r}, must be between 1 and 60 seconds "
            "(Telegram's self-destruct timer range)"
        )
    return value


def load_script(path: Path) -> List[List[ScriptMessage]]:
    """Load and validate a conversation script from JSON.

    Expected format:
        {"turns": [["msg1", "msg2"], ["msg3"], ...]}
    Each turn is a list of messages sent back to back: up to 3 *visible*
    messages (plain text, an object {"media": "media/photo1.jpg",
    "caption": "optional text"}, or an object {"text": "msg",
    "delete_after": 30} - a text message that auto-deletes itself that
    many seconds after being sent) - that cap keeps the simulated typing
    realistic - plus as many {"notify": "text"} entries as needed, since
    those are silent off-camera notifications sent via the notifier
    account instead of the scripted chat and carry no such realism
    constraint (e.g. a burst of several short "analysis" cards), plus as
    many {"me": "text"} entries as needed - auto-replies sent by YOUR own
    account (the "me" client) to Maria, which also don't count against
    the visible-message cap. Any visible (text/media) or "me" entry may
    also carry "wait_before_next": 10 to replace the usual randomised
    inter-message gap with an exact pause.
    """
    if not path.exists():
        raise FileNotFoundError(f"Script file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    raw_turns = data.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ValueError(f"Script {path} must contain a non-empty 'turns' list")

    turns: List[List[ScriptMessage]] = []
    for i, raw_turn in enumerate(raw_turns):
        if not isinstance(raw_turn, list) or not raw_turn:
            raise ValueError(
                f"Script {path}: turn #{i} must be a non-empty list of messages, got {raw_turn!r}"
            )
        visible_count = sum(1 for m in raw_turn if _is_visible_entry(m))
        if visible_count > 3:
            raise ValueError(
                f"Script {path}: turn #{i} has {visible_count} visible (non-notify, non-me) "
                "messages, max is 3 - split extra chat lines into a new turn "
                "('!notify' and '> text' me-replies don't count)"
            )

        turn: List[ScriptMessage] = []
        for raw_msg in raw_turn:
            if isinstance(raw_msg, str):
                if not raw_msg.strip():
                    raise ValueError(f"Script {path}: turn #{i} contains an empty text message")
                turn.append(ScriptMessage(text=raw_msg))
            elif isinstance(raw_msg, dict) and raw_msg.get("notify"):
                wait_before_next = _parse_optional_positive_float(
                    raw_msg, "wait_before_next", path, i
                )
                turn.append(
                    ScriptMessage(notify=str(raw_msg["notify"]), wait_before_next=wait_before_next)
                )
            elif isinstance(raw_msg, dict) and raw_msg.get("me"):
                me_text = raw_msg["me"]
                if not isinstance(me_text, str) or not me_text.strip():
                    raise ValueError(
                        f"Script {path}: turn #{i} has a 'me' entry with an empty/invalid text"
                    )
                wait_before_next = _parse_optional_positive_float(
                    raw_msg, "wait_before_next", path, i
                )
                turn.append(
                    ScriptMessage(text=me_text, is_me=True, wait_before_next=wait_before_next)
                )
            elif (
                isinstance(raw_msg, dict)
                and raw_msg.get("text") is not None
                and not raw_msg.get("media")
            ):
                text_value = raw_msg["text"]
                if not isinstance(text_value, str) or not text_value.strip():
                    raise ValueError(
                        f"Script {path}: turn #{i} has a 'text' entry with an empty/invalid text"
                    )
                delete_after = _parse_optional_positive_float(raw_msg, "delete_after", path, i)
                wait_before_next = _parse_optional_positive_float(
                    raw_msg, "wait_before_next", path, i
                )
                turn.append(
                    ScriptMessage(
                        text=text_value,
                        delete_after=delete_after,
                        wait_before_next=wait_before_next,
                    )
                )
            elif isinstance(raw_msg, dict) and raw_msg.get("media"):
                media_path = _resolve_media_path(str(raw_msg["media"]), path)
                if not media_path.exists():
                    raise ValueError(
                        f"Script {path}: turn #{i} references missing media file {media_path}"
                    )
                caption = raw_msg.get("caption")
                if caption is not None and not isinstance(caption, str):
                    raise ValueError(f"Script {path}: turn #{i} has a non-string caption")
                wait_before_next = _parse_optional_positive_float(
                    raw_msg, "wait_before_next", path, i
                )
                ttl = _parse_optional_ttl(raw_msg, path, i)
                turn.append(
                    ScriptMessage(
                        text=caption,
                        media=media_path,
                        wait_before_next=wait_before_next,
                        ttl=ttl,
                    )
                )
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
        send_ts = time.time()
        await notify_client.send_message(notify_target, message.notify)
        # Explicit wall-clock timestamp (independent of the logging
        # formatter's asctime settings) so actual gaps between back-to-back
        # '!notify' sends can be read straight off the console.
        logger.info(
            "Sent off-camera notification at %s (unix=%.3f): %r",
            time.strftime("%H:%M:%S", time.localtime(send_ts)) + f".{int(send_ts % 1 * 1000):03d}",
            send_ts,
            message.notify,
        )
        return

    duration = compute_action_duration(message, cfg)

    if message.media is not None:
        action_type = _media_action_type(message.media)
        logger.debug(
            "Chat action %r for %.2fs before sending media: %s", action_type, duration, message.media
        )
        async with client.action(chat_id, action_type):
            await asyncio.sleep(duration)
        if message.ttl is not None:
            try:
                await client.send_file(
                    chat_id, str(message.media), caption=message.text, ttl=message.ttl
                )
                logger.info(
                    "Sent self-destructing media (ttl=%ds) to chat %s: %s (caption=%r)",
                    message.ttl,
                    chat_id,
                    message.media,
                    message.text,
                )
            except TtlMediaInvalidError:
                logger.exception(
                    "Telegram rejected ttl=%ds for %s (self-destruct timer only works for "
                    "photos/videos in private chats); sending without a timer instead.",
                    message.ttl,
                    message.media,
                )
                await client.send_file(chat_id, str(message.media), caption=message.text)
                logger.info(
                    "Sent media to chat %s: %s (caption=%r)", chat_id, message.media, message.text
                )
        else:
            await client.send_file(chat_id, str(message.media), caption=message.text)
            logger.info("Sent media to chat %s: %s (caption=%r)", chat_id, message.media, message.text)
    else:
        logger.debug("Typing indicator for %.2fs before sending: %r", duration, message.text)
        async with client.action(chat_id, "typing"):
            await asyncio.sleep(duration)
        sent_msg = await client.send_message(chat_id, message.text)
        logger.info("Sent message to chat %s: %r", chat_id, message.text)
        if message.delete_after is not None:
            # Fire-and-forget: must not block playback of the rest of the
            # turn (e.g. a '!notify' right after) while waiting to delete.
            asyncio.create_task(
                _delete_after_delay(client, chat_id, sent_msg.id, message.delete_after)
            )


async def _delete_after_delay(client: TelegramClient, chat_id, message_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        await client.delete_messages(chat_id, message_id)
        logger.info("Auto-deleted message %s in chat %s after %.1fs", message_id, chat_id, delay)
    except Exception:  # noqa: BLE001 - background task, must never crash the process
        logger.exception("Failed to auto-delete message %s in chat %s", message_id, chat_id)


def _compute_me_typing_duration(text: str) -> float:
    raw = 0.05 * len(text) + random.uniform(0.4, 1.2)
    return max(1.0, min(raw, 7.0))


async def _send_me_reply(
    me_client: TelegramClient,
    maria_peer,
    message: ScriptMessage,
    preceded_by_notify: bool,
):
    """Send one '> text' auto-reply as YOUR OWN account (the "me" client)
    to Maria - its own read-pause/typing pacing, independent of Maria's."""
    read_pause = random.uniform(1.5, 3.0)
    if preceded_by_notify:
        # Give the off-camera '!notify' push time to actually land on
        # screen before "you" fire back a reply to Maria.
        read_pause += NOTIFY_READ_DELAY
    logger.debug("Me-client waiting %.2fs to simulate reading before replying", read_pause)
    await asyncio.sleep(read_pause)

    typing_time = _compute_me_typing_duration(message.text or "")
    async with me_client.action(maria_peer, "typing"):
        await asyncio.sleep(typing_time)

    try:
        sent_msg = await me_client.send_message(maria_peer, message.text)
    except FloodWaitError as exc:
        logger.warning(
            "Me-client hit a FloodWaitError sending %r; waiting %ds as Telegram asked, "
            "then retrying once.",
            message.text,
            exc.seconds,
        )
        await asyncio.sleep(exc.seconds)
        sent_msg = await me_client.send_message(maria_peer, message.text)

    logger.info("Sent me-reply to Maria: %r", message.text)
    return sent_msg


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
        me_client: Optional[TelegramClient] = None,
        maria_peer=None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.script_name = script_name
        self.turns = turns
        self.state_store = state_store
        self.notify_client = notify_client
        self.notify_target = notify_target
        self.me_client = me_client
        self.maria_peer = maria_peer
        # Ids of messages the "me" client itself sent to Maria. The main
        # incoming-message handler listens for messages from the TARGET
        # account - but the "me" client IS logged into that same account,
        # so without this it would mistake its own auto-reply for a new
        # real message and re-trigger playback on itself.
        self.me_sent_message_ids: set = set()
        # In private chats Telegram gives the SAME message different ids on
        # the sender's and the recipient's side, so the id set above alone
        # can't recognise the echo as seen by Maria's client. Also remember
        # (text, expiry) of every pending/just-sent me-reply and match the
        # echo by content. Registered BEFORE sending so an echo that races
        # ahead of send_message()'s return is still caught.
        self._me_pending_echoes: List[tuple] = []
        self._me_replies_sent_count = 0
        self._lock = asyncio.Lock()

    def register_me_echo(self, text: str) -> None:
        self._me_pending_echoes.append((text, time.monotonic() + ME_ECHO_TTL_SECONDS))

    def unregister_me_echo(self, text: str) -> None:
        for idx, (pending_text, _) in enumerate(self._me_pending_echoes):
            if pending_text == text:
                del self._me_pending_echoes[idx]
                return

    def is_me_echo(self, event_id: int, text: str) -> bool:
        """True if this incoming event is just our own "me" client's reply
        arriving back through Maria's client (same underlying account as
        the target) - not a real message that should trigger playback."""
        if event_id in self.me_sent_message_ids:
            return True
        now = time.monotonic()
        self._me_pending_echoes = [(t, exp) for t, exp in self._me_pending_echoes if exp > now]
        for idx, (pending_text, _) in enumerate(self._me_pending_echoes):
            if pending_text == text:
                del self._me_pending_echoes[idx]
                return True
        return False

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
            self._me_replies_sent_count = 0
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

            # Keep auto-advancing through consecutive turns that end in a
            # successfully-sent '> text' me-reply, since that reply stands
            # in for the real incoming message that would normally trigger
            # the next turn. Only stop once a turn ends on an ordinary
            # (non-me) message or the script runs out - this is what keeps
            # auto and manual turns mixable in the same scenario.
            while True:
                ended_with_me_reply = await self._play_turn(chat_id, state, log_chat_id)
                if not ended_with_me_reply:
                    break
                state = self._load_state()
                if state.turn_index >= len(self.turns):
                    break

    async def _play_turn(self, chat_id, state: ScriptState, log_chat_id: int) -> bool:
        """Play back one turn. Returns True if the LAST message actually
        sent in this turn was a successfully-sent '> text' me-reply - the
        caller uses this to decide whether to immediately auto-advance
        into the next turn (the me-reply stands in for a real incoming
        message) instead of waiting for one."""
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

        ended_with_sent_me_reply = False

        for i in range(state.message_index, len(turn)):
            message = turn[i]
            ended_with_sent_me_reply = False

            if message.is_me:
                if self.me_client is None or self.maria_peer is None:
                    logger.warning(
                        "Script %r turn %d has a '> %s' me-reply but ME_SESSION_STRING/"
                        "MARIA_PEER isn't configured; skipping it (waiting for your real "
                        "reply as usual).",
                        self.script_name,
                        state.turn_index + 1,
                        message.text,
                    )
                elif self._me_replies_sent_count >= ME_MAX_REPLIES_PER_SCRIPT:
                    logger.warning(
                        "Reached the %d-message safety cap on '> text' auto-replies for "
                        "script %r; skipping this and any further me-replies.",
                        ME_MAX_REPLIES_PER_SCRIPT,
                        self.script_name,
                    )
                else:
                    preceded_by_notify = i > 0 and turn[i - 1].notify is not None
                    self.register_me_echo(message.text or "")
                    try:
                        sent_msg = await _send_me_reply(
                            self.me_client, self.maria_peer, message, preceded_by_notify
                        )
                    except Exception:
                        self.unregister_me_echo(message.text or "")
                        logger.exception(
                            "Failed to send me-reply %d/%d of turn %d/%d for script %r: %r",
                            i + 1,
                            len(turn),
                            state.turn_index + 1,
                            len(self.turns),
                            self.script_name,
                            message,
                        )
                        raise
                    self.me_sent_message_ids.add(sent_msg.id)
                    self._me_replies_sent_count += 1
                    ended_with_sent_me_reply = True
            else:
                try:
                    await send_single_message(
                        self.client,
                        chat_id,
                        message,
                        self.cfg,
                        notify_client=self.notify_client,
                        notify_target=self.notify_target,
                    )
                except Exception:
                    # Pinpoint exactly which turn/message failed so this shows
                    # up directly in the logs instead of having to infer it
                    # from the state file after the fact - then re-raise so
                    # the caller's existing error handling still applies.
                    logger.exception(
                        "Failed to send message %d/%d of turn %d/%d for script %r: %r",
                        i + 1,
                        len(turn),
                        state.turn_index + 1,
                        len(self.turns),
                        self.script_name,
                        message,
                    )
                    raise
            # Save after every individual message so a crash mid-turn
            # resumes from the next unsent message, not from scratch.
            self._save_state(ScriptState(turn_index=state.turn_index, message_index=i + 1))

            if i < len(turn) - 1:
                if turn[i].wait_before_next is not None:
                    # '!wait <seconds>' replaces the usual randomised
                    # typing-pace gap with an exact pause. If the message
                    # also auto-deletes ('!delete'), make sure the pause is
                    # at least long enough for the delete to actually
                    # happen (plus a notify-style buffer) before moving on.
                    gap = turn[i].wait_before_next
                    if turn[i].delete_after is not None:
                        gap = max(gap, turn[i].delete_after + self.cfg.notify_gap_delay)
                    logger.info(
                        "Waiting wait_before_next=%.2fs before next message (turn %d, message %d->%d)",
                        gap,
                        state.turn_index + 1,
                        i + 1,
                        i + 2,
                    )
                elif turn[i].notify is not None and turn[i + 1].notify is not None:
                    # Back-to-back '!notify' entries (e.g. a burst of short
                    # "analysis"/"date plan" cards) get their own, slower,
                    # fixed gap so each one has time to be read - independent
                    # of the normal in-chat typing pacing.
                    gap = self.cfg.notify_gap_delay
                    logger.info(
                        "Waiting notify_gap_delay=%.2fs before next '!notify' (turn %d, message %d->%d)",
                        gap,
                        state.turn_index + 1,
                        i + 1,
                        i + 2,
                    )
                else:
                    gap = random.uniform(self.cfg.inter_message_delay_min, self.cfg.inter_message_delay_max)
                await asyncio.sleep(gap)

        if ended_with_sent_me_reply and turn[-1].wait_before_next is not None:
            # A '!wait' after the turn's last line (a me-reply) has no "next
            # message in this turn" to delay, but this turn auto-continues
            # into the next one - so honour the pause before that happens.
            logger.info(
                "Waiting wait_before_next=%.2fs before auto-continuing to the next turn",
                turn[-1].wait_before_next,
            )
            await asyncio.sleep(turn[-1].wait_before_next)

        self._save_state(ScriptState(turn_index=state.turn_index + 1, message_index=0))
        return ended_with_sent_me_reply

    def reset(self) -> None:
        self._me_replies_sent_count = 0
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

# How long to wait after the LAST new authoring-chat message before
# re-parsing it, so a multi-message scenario is reloaded once, not per line.
AUTHORING_RELOAD_DEBOUNCE = 2.0


async def _reload_active_script_from_authoring_chat(
    client: TelegramClient,
    cfg: Config,
    player: "ScriptPlayer",
    authoring_entity: str,
    extra_author_ids: set,
) -> Optional[float]:
    """Re-parse the authoring chat and, if it defines any '# name' scenario,
    make the most recently written one the active script - so the moment
    you finish sending a scenario there, the bot is ready to play it back
    on the very next message from the target.

    A scenario with '<фото>'/'<видео>' placeholders is NOT activated until
    all its media has arrived (see media_wait.py); in that case the bot
    answers in the authoring chat instead. Returns how many seconds from
    now to re-run this (the media-wait timeout), or None."""
    meta: dict = {}
    scripts = await fetch_scripts_from_chat(
        client, cfg, authoring_entity, AUTHORING_FETCH_LIMIT, extra_author_ids, meta
    )
    if not scripts:
        return None

    name = next(reversed(scripts))

    async def say(text: str) -> bool:
        return await _say_in_authoring_chat(client, authoring_entity, text)

    blocked, recheck_in, just_completed = await media_wait.announce(
        say, cfg.state_dir, name, meta.get(name)
    )
    if blocked:
        logger.info("Scenario %r is waiting for media (or was cancelled); not switching yet.", name)
        return recheck_in

    turns_raw = scripts[name]
    if not turns_raw:
        logger.warning("Latest scenario %r in authoring chat has no turns yet; not switching.", name)
        return None

    per_turn_counts = [
        (
            sum(1 for m in turn if isinstance(m, dict) and m.get("media")),
            sum(1 for m in turn if not (isinstance(m, dict) and m.get("media"))),
        )
        for turn in turns_raw
    ]
    logger.info(
        "Parsed scenario %r: %d turns, media/text per turn: %s",
        name,
        len(turns_raw),
        ", ".join(f"#{i + 1}={media}m/{text}t" for i, (media, text) in enumerate(per_turn_counts)),
    )

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
    media_wait.set_last_activated(cfg.state_dir, name)
    if just_completed:
        media_wait.mark_completed(cfg.state_dir)
        await say(f"Готово, сценарий «{name}» запущен")
    return None


async def _say_in_authoring_chat(client: TelegramClient, entity, text: str) -> bool:
    """Post a bot notice into the authoring chat. Prefixed so the re-parse
    (which also reads our own outgoing messages) can recognise and skip it."""
    try:
        await client.send_message(entity, f"{BOT_NOTICE_PREFIX} {text}")
        return True
    except Exception:  # noqa: BLE001 - a failed notice must never break a reload
        logger.exception("Could not post a notice in the authoring chat")
        return False


async def _send_media_status(
    client: TelegramClient, cfg: Config, player: "ScriptPlayer", entity: str, extra_author_ids: set
) -> None:
    """'/status': what is the latest scenario still missing?"""
    meta: dict = {}
    scripts = await fetch_scripts_from_chat(
        client, cfg, entity, AUTHORING_FETCH_LIMIT, extra_author_ids, meta
    )
    name = next(reversed(scripts)) if scripts else None
    await _say_in_authoring_chat(
        client, entity, media_wait.status_text(meta.get(name) if name else None, player.script_name)
    )


async def _rerun_last_scenario(
    client: TelegramClient, cfg: Config, player: "ScriptPlayer", entity: str
) -> None:
    """'/rerun': replay the last activated scenario from turn 0, reusing the
    media files already saved on disk - nothing needs to be re-sent."""
    name = media_wait.load_state(cfg.state_dir).get("last_activated")
    path = cfg.scripts_dir / f"{name}.json" if name else None
    if not path or not path.exists():
        await _say_in_authoring_chat(client, entity, "Нет запущенного сценария для перезапуска")
        return
    await player.load_script(name, load_script(path))
    await _say_in_authoring_chat(client, entity, f"Сценарий «{name}» перезапущен с начала")


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

    me_client: Optional[TelegramClient] = None
    maria_peer = None
    if cfg.me_session_string:
        if not cfg.maria_peer:
            logger.error(
                "ME_SESSION_STRING is set but MARIA_PEER is not - the \"me\" client "
                "wouldn't know who to send '> text' replies to. Auto-replies are "
                "disabled for now."
            )
        else:
            me_client = TelegramClient(
                StringSession(cfg.me_session_string), cfg.api_id, cfg.api_hash
            )
            await me_client.connect()
            if not await me_client.is_user_authorized():
                logger.error(
                    "ME_SESSION_STRING is not authorized (expired/revoked?). Generate a "
                    "fresh one with 'python make_me_session.py'. '> text' auto-replies "
                    "are disabled for now."
                )
                await me_client.disconnect()
                me_client = None
            else:
                maria_peer_raw = cfg.maria_peer.strip()
                if maria_peer_raw.lstrip("-").isdigit():
                    maria_peer = int(maria_peer_raw)
                elif maria_peer_raw.startswith("+"):
                    maria_peer = await me_client.get_entity(maria_peer_raw)
                else:
                    maria_peer = await me_client.get_entity(
                        maria_peer_raw if maria_peer_raw.startswith("@") else f"@{maria_peer_raw}"
                    )
                logger.info(
                    "Me-client ready; '> text' auto-replies will be sent to %r", cfg.maria_peer
                )
    elif any(msg.is_me for turn in turns for msg in turn):
        logger.warning(
            "Script %r has '> text' me-replies but ME_SESSION_STRING isn't set in .env; "
            "those lines will be skipped and the bot will wait for your real replies "
            "instead. Run 'python make_me_session.py' to generate one.",
            script_name,
        )

    player = ScriptPlayer(
        client,
        cfg,
        script_name,
        turns,
        state_store,
        notify_client,
        notify_target,
        me_client,
        maria_peer,
    )

    @client.on(events.NewMessage(incoming=True))
    async def _handler(event: events.NewMessage.Event) -> None:  # noqa: ANN401
        sender_id = event.sender_id
        if sender_id != target_id:
            return
        if player.is_me_echo(event.id, (event.raw_text or "").strip()):
            # This is the "me" client's own auto-reply arriving back as an
            # "incoming" update (same underlying account as the target) -
            # not a real reply, so don't let it re-trigger playback.
            logger.debug("Ignoring echo of our own me-reply (event id %s)", event.id)
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

        # The reload re-reads the whole authoring chat and can take several
        # seconds. Running it inside the update handler would block every
        # other update (client uses sequential_updates=True) - including the
        # target's message that Maria should be answering - so it runs as a
        # background task instead. It's also debounced: each new authoring
        # message cancels the pending reload and restarts a short timer, so
        # typing a scenario line by line triggers ONE reload at the end
        # rather than one per line.
        reload_state: dict = {"task": None, "recheck": None}

        def _cancel(task_key: str) -> None:
            task = reload_state[task_key]
            if task is not None and not task.done():
                task.cancel()

        async def _recheck_later(delay: float) -> None:
            # A scenario waiting for media must time out even if nothing new
            # is sent to the chat, so re-run the reload when the wait expires.
            try:
                await asyncio.sleep(delay)
                await _reload_active_script_from_authoring_chat(
                    client, cfg, player, authoring_entity, authoring_extra_author_ids
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the bot alive on reload errors
                logger.exception("Error while re-checking the media wait")

        async def _debounced_reload() -> None:
            try:
                await asyncio.sleep(AUTHORING_RELOAD_DEBOUNCE)
                recheck_in = await _reload_active_script_from_authoring_chat(
                    client, cfg, player, authoring_entity, authoring_extra_author_ids
                )
                if recheck_in:
                    _cancel("recheck")
                    reload_state["recheck"] = asyncio.create_task(_recheck_later(recheck_in))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the bot alive on reload errors
                logger.exception("Error while reloading script from authoring chat")

        @client.on(events.NewMessage(chats=authoring_entity))
        async def _authoring_handler(event: events.NewMessage.Event) -> None:  # noqa: ANN401
            if not (event.out or event.sender_id in authoring_extra_author_ids):
                return  # ignore anyone else who might post in that chat
            text = (event.raw_text or "").strip()
            if text.startswith(BOT_NOTICE_PREFIX):
                return  # our own notice - nothing to reload
            if text == "/status":
                asyncio.create_task(
                    _send_media_status(
                        client, cfg, player, authoring_entity, authoring_extra_author_ids
                    )
                )
                return
            if text == "/rerun":
                asyncio.create_task(_rerun_last_scenario(client, cfg, player, authoring_entity))
                return
            _cancel("task")
            _cancel("recheck")
            reload_state["task"] = asyncio.create_task(_debounced_reload())

        # Pick up the latest scenario from the chat on startup too. Without
        # this a restart falls back to the --script given on the command
        # line until something new is written to the chat, so a scenario
        # prepared before the restart silently "doesn't start".
        reload_state["task"] = asyncio.create_task(_debounced_reload())

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
        if me_client is not None:
            await me_client.disconnect()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """scripted-reply-userbot — plays back a scripted Telegram conversation."""


def print_dry_run_timeline(cfg: Config, turns: List[List[ScriptMessage]]) -> None:
    """Print a readable timeline of a script's pacing - who sends what,
    with delays - without connecting to Telegram or sending anything.
    Useful for sanity-checking a scenario (especially '> text' me-reply
    auto-advance chains) before actually running it."""
    click.echo(f"Dry run: {len(turns)} turn(s)\n")
    for t_idx, turn in enumerate(turns):
        click.echo(f"=== Turn {t_idx + 1}/{len(turns)} ===")
        click.echo(
            f"  [read delay ~{cfg.read_delay_min:.1f}-{cfg.read_delay_max:.1f}s before turn starts]"
        )
        for i, message in enumerate(turn):
            if message.notify is not None:
                click.echo(f"  !notify -> notifier: {message.notify!r}")
            elif message.is_me:
                read_pause_desc = "1.5-3.0s"
                if i > 0 and turn[i - 1].notify is not None:
                    read_pause_desc = f"1.5-3.0s + NOTIFY_READ_DELAY={NOTIFY_READ_DELAY:.1f}s"
                text = message.text or ""
                min_typing = max(1.0, min(0.05 * len(text) + 0.4, 7.0))
                max_typing = max(1.0, min(0.05 * len(text) + 1.2, 7.0))
                click.echo(
                    f"  > (me, read-pause {read_pause_desc}, typing ~{min_typing:.1f}-"
                    f"{max_typing:.1f}s): {text!r}"
                )
            elif message.media is not None:
                ttl_desc = f", ttl={message.ttl}s" if message.ttl else ""
                click.echo(f"  [media] {message.media}{ttl_desc} (caption={message.text!r})")
            else:
                typing = compute_typing_duration(message.text or "", cfg)
                click.echo(f"  Maria (typing ~{typing:.1f}s): {message.text!r}")

            if message.delete_after is not None:
                click.echo(f"      -> auto-deletes after {message.delete_after:.1f}s")

            if i < len(turn) - 1:
                if message.wait_before_next is not None:
                    gap = message.wait_before_next
                    if message.delete_after is not None:
                        gap = max(gap, message.delete_after + cfg.notify_gap_delay)
                    click.echo(f"  [wait {gap:.1f}s - wait_before_next]")
                elif message.notify is not None and turn[i + 1].notify is not None:
                    click.echo(f"  [wait {cfg.notify_gap_delay:.1f}s - notify_gap_delay]")
                else:
                    click.echo(
                        f"  [wait ~{cfg.inter_message_delay_min:.1f}-"
                        f"{cfg.inter_message_delay_max:.1f}s]"
                    )

        if turn and turn[-1].is_me:
            if turn[-1].wait_before_next is not None:
                click.echo(f"  [wait {turn[-1].wait_before_next:.1f}s - wait_before_next]")
            click.echo("  -> turn ends with a me-reply: auto-continues to next turn\n")
        else:
            click.echo("  -> waiting for your real reply before next turn\n")


@cli.command("run")
@click.option(
    "--script",
    "script_arg",
    required=True,
    help="Path to a script JSON file, e.g. scripts/demo.json",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=lambda: bool(os.getenv("DRY_RUN")),
    help="Don't connect to Telegram or send anything - just print the scenario's "
    "timeline (who sends what, delays, notify) and exit. Same as DRY_RUN=1.",
)
def run_command(script_arg: str, dry_run: bool) -> None:
    """Start listening and playing back the given script."""
    try:
        cfg = load_config()
        script_path = _resolve_script_path(cfg, script_arg)
        if dry_run:
            turns = load_script(script_path)
            print_dry_run_timeline(cfg, turns)
            return
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
MEDIA_REF_PREFIX = "!media"
DELETE_PREFIX = "!delete"
WAIT_PREFIX = "!wait"
TIMER_PREFIX = "!timer"
ME_PREFIX = "> "


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
    meta: Optional[dict] = None,
) -> "dict[str, List[List[dict]]]":
    """Fetch and parse every '# name' scenario section out of a chat's
    message history. Shared by the one-shot 'export-script' command and the
    live authoring-chat watcher in 'run'. `meta`, if given, is filled with
    per-scenario media-wait bookkeeping (see media_wait.py).
    """
    extra_author_ids = extra_author_ids or set()
    entity = "me" if from_chat.strip().lower() == "me" else from_chat.strip()
    messages = [
        msg
        async for msg in client.iter_messages(entity, limit=limit, reverse=True)
        if (msg.out or msg.sender_id in extra_author_ids)
        and (msg.text or msg.media)
        and not (msg.text or "").startswith(BOT_NOTICE_PREFIX)
    ]
    return await _parse_scripts_from_messages(client, cfg, messages, meta)


async def _parse_scripts_from_messages(
    client: TelegramClient, cfg: Config, messages: list, meta: Optional[dict] = None
) -> "dict[str, List[List[dict]]]":
    # A single Telegram message may itself contain multiple newline-separated
    # lines (very common when typing on a phone: each Enter starts a new
    # line, and the whole block is sent as one message). Flatten every
    # message into a stream of tokens - one per line for text, one for each
    # media message - so '# name' headers and '-' delimiters are recognised
    # regardless of whether they arrived as separate messages or as lines
    # within one message.
    if meta is None:
        meta = {}
    tokens: List[tuple] = []
    for msg in messages:
        if msg.media:
            caption = (msg.text or "").strip() or None
            tokens.append(("media", msg, caption))
        else:
            for line in (msg.text or "").splitlines():
                line = line.strip()
                if line:
                    tokens.append(("text", line, msg))

    scripts: "dict[str, List[List[dict]]]" = {}
    current_name: Optional[str] = None

    for token in tokens:
        kind = token[0]
        text = token[1] if kind == "text" else None

        if kind == "text" and text.startswith(SCRIPT_HEADER_PREFIX):
            current_name = _slugify_script_name(text[len(SCRIPT_HEADER_PREFIX):])
            if current_name in scripts:
                # Re-used name (e.g. you rewrote the same '# demo' scene
                # again later): drop it first so re-inserting below moves
                # it to the END of the dict. Without this, a plain dict
                # keeps a key's ORIGINAL insertion position even after its
                # value is reassigned, so '_reload_active_script_from_
                # authoring_chat' (which picks next(reversed(scripts)) as
                # "the most recently written scenario") would keep
                # activating some other, actually-older scenario instead
                # of the one you just rewrote.
                del scripts[current_name]
            scripts[current_name] = [[]]
            meta[current_name] = media_wait.new_meta(token[2])
            continue

        if current_name is None:
            continue  # ignore stray content before the first '# name' header

        turns = scripts[current_name]
        info = meta[current_name]

        if kind == "text" and text in MEDIA_COMMANDS:
            # '/skip' leaves the next waiting placeholder empty, '/cancel'
            # drops the scenario that is still waiting for media; '/status'
            # and '/rerun' are acted on by the chat handler. None of them is
            # scenario text.
            if text == "/skip":
                pending = media_wait.first_pending(info)
                if pending is not None:
                    pending["state"] = "skipped"
            elif text == "/cancel" and media_wait.first_pending(info) is not None:
                info["cancelled"] = True
            continue

        if info["cancelled"]:
            continue  # everything after '/cancel' belongs to no scenario

        if kind == "text" and text == TURN_DELIMITER:
            if turns[-1]:
                turns.append([])
            continue

        placeholder = media_wait.parse_placeholder(text) if kind == "text" else None
        if placeholder is not None:
            # '<фото>' / '<видео>' [caption] - the real file is sent AFTER the
            # scenario text and substituted in order (see media_wait.py).
            if sum(1 for m in turns[-1] if _is_visible_entry(m)) >= 3:
                turns.append([])
            ph_kind, ph_caption = placeholder
            ph_entry: dict = {"placeholder": ph_kind}
            if ph_caption:
                ph_entry["caption"] = ph_caption
            turns[-1].append(ph_entry)
            info["placeholders"].append({"kind": ph_kind, "state": "pending", "entry": ph_entry})
            continue

        if kind == "text" and text.startswith(ME_PREFIX):
            # '> text' - an auto-reply sent by the "me" client (your own
            # account) to Maria, interleaved in normal message order.
            # Doesn't count against the 3-visible-message cap.
            me_text = text[len(ME_PREFIX):].strip()
            if me_text:
                turns[-1].append({"me": me_text})
            else:
                logger.warning(
                    "Scenario %r has an empty '> ' (me-reply) line; skipping.",
                    current_name,
                )
            continue

        if kind == "text" and text.startswith(NOTIFY_PREFIX):
            # '!notify' entries are silent/off-camera - they don't count
            # against the 3-visible-message-per-turn cap, so a burst of
            # several of them never forces an extra turn boundary here.
            notify_text = text[len(NOTIFY_PREFIX):].strip()
            if notify_text:
                turns[-1].append({"notify": notify_text})
            continue

        if kind == "text" and text.startswith(DELETE_PREFIX):
            # '!delete <seconds>' attaches an auto-delete timer to the
            # message that immediately precedes it - must be a plain text
            # message (not media, not notify), and only one per message.
            rest = text[len(DELETE_PREFIX):].strip()
            try:
                seconds = _leading_number(rest, DELETE_PREFIX, current_name)
                if seconds <= 0:
                    raise ValueError("delete_after must be positive")
            except ValueError:
                logger.warning(
                    "Scenario %r has '!delete %s' with an invalid/non-positive duration; "
                    "skipping the marker.",
                    current_name,
                    rest,
                )
                continue
            if turns[-1] and isinstance(turns[-1][-1], str):
                turns[-1][-1] = {"text": turns[-1][-1], "delete_after": seconds}
            else:
                logger.warning(
                    "Scenario %r has '!delete' that doesn't directly follow a plain text "
                    "message (must come right after a text line - not media/notify/me - and "
                    "only once per message); skipping the marker.",
                    current_name,
                )
            continue

        if kind == "text" and text.startswith(WAIT_PREFIX):
            # '!wait <seconds>' sets an exact pause before the NEXT message
            # in the turn, replacing the usual randomised typing-pace gap.
            # Attaches to the message that immediately precedes it - any
            # visible message (text or media), but not a '!notify' entry.
            rest = text[len(WAIT_PREFIX):].strip()
            try:
                seconds = _leading_number(rest, WAIT_PREFIX, current_name)
                if seconds <= 0:
                    raise ValueError("wait_before_next must be positive")
            except ValueError:
                logger.warning(
                    "Scenario %r has '!wait %s' with an invalid/non-positive duration; "
                    "skipping the marker.",
                    current_name,
                    rest,
                )
                continue
            if turns[-1]:
                # Attaches to whatever came right before it - a visible
                # message, a '> text' me-reply, or a '!notify' (the pause
                # then happens after the notification, before the next line).
                last_entry = turns[-1][-1]
                if isinstance(last_entry, str):
                    turns[-1][-1] = {"text": last_entry, "wait_before_next": seconds}
                else:
                    last_entry["wait_before_next"] = seconds
            else:
                logger.warning(
                    "Scenario %r has '!wait' at the very start of a turn - there is "
                    "nothing before it to pause after; skipping the marker.",
                    current_name,
                )
            continue

        if kind == "text" and text.startswith(TIMER_PREFIX):
            # '!timer <seconds>' sends the media that immediately precedes
            # it as Telegram self-destructing media (1-60s view timer).
            # Must directly follow a media entry - not text, not notify.
            rest = text[len(TIMER_PREFIX):].strip()
            try:
                ttl_seconds = int(_leading_number(rest, TIMER_PREFIX, current_name))
                if not 1 <= ttl_seconds <= 60:
                    raise ValueError("ttl must be between 1 and 60 seconds")
            except ValueError:
                logger.warning(
                    "Scenario %r has '!timer %s' with an invalid duration (must be "
                    "a whole number of seconds, 1-60); skipping the marker.",
                    current_name,
                    rest,
                )
                continue
            if turns[-1] and _is_media_entry(turns[-1][-1]):
                turns[-1][-1]["ttl"] = ttl_seconds
            else:
                logger.warning(
                    "Scenario %r has '!timer' that doesn't directly follow a media "
                    "message (photo/video); skipping the marker.",
                    current_name,
                )
            continue

        if kind == "text" and text.startswith(MEDIA_REF_PREFIX):
            # '!media <path>' or '!media <path> | <caption>' - reference a
            # file already sitting on disk instead of attaching it live.
            # Counts as a visible message, same as a real attachment.
            if sum(1 for m in turns[-1] if _is_visible_entry(m)) >= 3:
                turns.append([])
            rest = text[len(MEDIA_REF_PREFIX):].strip()
            if "|" in rest:
                raw_path, caption = rest.split("|", 1)
                raw_path, caption = raw_path.strip(), caption.strip() or None
            else:
                raw_path, caption = rest, None
            resolved = _resolve_referenced_media(raw_path, cfg)
            if resolved is None:
                logger.warning(
                    "Scenario references '!media %s' but no such file was found "
                    "(checked scripts/media/, scripts/, and the project root); skipping.",
                    raw_path,
                )
            else:
                entry: dict = {"media": str(resolved)}
                if caption:
                    entry["caption"] = caption
                turns[-1].append(entry)
            continue

        if kind == "media":
            waiting = media_wait.first_pending(info)
            if waiting is not None:
                # This scenario is still waiting for media: the file fills
                # the next placeholder instead of being appended inline.
                _, msg, media_caption = token
                if media_wait.media_kind(msg) != waiting["kind"]:
                    info["wrong"].append({"id": msg.id, "expected": waiting["kind"]})
                    continue
                media_dir = cfg.scripts_dir / "media" / current_name
                media_dir.mkdir(parents=True, exist_ok=True)
                try:
                    saved_path = await _download_message_media(client, msg, media_dir)
                except Exception as exc:  # noqa: BLE001 - leave the placeholder waiting
                    logger.warning(
                        "Failed to download media for a placeholder in scenario %r (%s: %s)",
                        current_name,
                        type(exc).__name__,
                        exc,
                    )
                    continue
                ph = waiting["entry"]
                ph.pop("placeholder", None)
                ph["media"] = f"media/{current_name}/{saved_path.name}"
                if media_caption and "caption" not in ph:
                    ph["caption"] = media_caption
                waiting["state"] = "filled"
                continue

        if sum(1 for m in turns[-1] if _is_visible_entry(m)) >= 3:
            turns.append([])

        if kind == "media":
            _, msg, caption = token
            media_dir = cfg.scripts_dir / "media" / current_name
            media_dir.mkdir(parents=True, exist_ok=True)
            try:
                saved_path = await _download_message_media(client, msg, media_dir)
            except Exception as exc:  # noqa: BLE001 - one bad photo shouldn't kill the whole reload
                logger.warning(
                    "Failed to download media from message id=%s date=%s in scenario %r "
                    "(%s: %s); skipping this photo, rest of the scenario continues.",
                    getattr(msg, "id", "?"),
                    getattr(msg, "date", "?"),
                    current_name,
                    type(exc).__name__,
                    exc,
                )
            else:
                entry: dict = {"media": f"media/{current_name}/{saved_path.name}"}
                if caption:
                    entry["caption"] = caption
                turns[-1].append(entry)
        else:
            turns[-1].append(text)

    for scenario_name, scenario_turns in scripts.items():
        scenario_info = meta.get(scenario_name)
        if scenario_info and not media_wait.is_unresolved(scenario_info):
            media_wait.finalize_scenario(scenario_turns, scenario_info)

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
    meta: dict = {}
    scripts = await fetch_scripts_from_chat(client, cfg, from_chat, limit, extra_author_ids, meta)
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
        if media_wait.is_unresolved(meta.get(name)):
            logger.warning(
                "Section %r still has unfilled <фото>/<видео> placeholders (or was "
                "cancelled); send its media in the chat first. Skipping.",
                name,
            )
            continue
        out_path = cfg.scripts_dir / f"{name}.json"
        out_path.write_text(
            json.dumps({"turns": turns}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        load_script(out_path)  # validate against the same loader the bot uses
        total_messages = sum(len(t) for t in turns)
        click.echo(f"Exported {len(turns)} turns ({total_messages} messages) to {out_path}")


async def _download_message_media(client: TelegramClient, msg, media_dir: Path) -> Path:
    """Download a message's media once and reuse it on every later reload.

    The authoring chat is re-parsed from scratch on every new message, so
    without a cache each reload re-downloaded every photo (slow, and with a
    directory target Telethon keeps adding ' (1)', ' (2)' ... duplicates).
    The file is named after the message id, which is stable, so an existing
    file means it's already been downloaded."""
    # Only a COMPLETE file may ever sit under its final name: the reload can
    # be cancelled mid-download (debounce), and a leftover empty/partial file
    # that the cache then trusted made every later send fail with
    # FilePartsInvalidError. So download to a '.part' file and rename it only
    # once finished, and never trust a zero-size cached file.
    for candidate in sorted(media_dir.glob(f"msg{msg.id}.*")):
        if candidate.suffix == ".part":
            continue
        if candidate.stat().st_size > 0:
            return candidate
        candidate.unlink()  # empty leftover from an interrupted download

    ext = (getattr(getattr(msg, "file", None), "ext", None) or ".jpg")
    final = media_dir / f"msg{msg.id}{ext}"
    part = media_dir / f"msg{msg.id}.part"
    part.unlink(missing_ok=True)
    saved = await client.download_media(msg, file=str(part))
    if not saved or not Path(saved).exists() or Path(saved).stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded media for message {msg.id} is empty or missing")
    os.replace(saved, final)
    return final


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
