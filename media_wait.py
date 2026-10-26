"""Collecting a scenario's media AFTER its text: '<фото>' / '<видео>' placeholders.

You send the scenario text with placeholder lines (optionally with a
caption after the tag), the bot asks for the media, you send them next, and
the bot fills the placeholders in order. There is no separate "pending"
state machine: the authoring chat's history is re-parsed on every change
anyway, so placeholders are simply filled during that parse from the media
that arrived after the scenario text (and '/skip' / '/cancel' are read from
the same history). That makes the whole thing restart-proof for free.

This module holds the pieces that don't need Telethon: the per-scenario
bookkeeping ("meta"), the small json remembering what the bot has already
announced (so a re-parse never repeats a reply), and the announcement logic.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Tuple

logger = logging.getLogger("scripted_reply_userbot")

# '<фото>' / '<видео>' (also <photo>/<video>), caption optional after the tag.
PLACEHOLDER_RE = re.compile(r"^<\s*(фото|видео|photo|video)\s*>\s*(.*)$", re.IGNORECASE)

# Lines the parser must never treat as scenario text. '/skip' and '/cancel'
# are also meaningful to the parse itself; '/status' and '/rerun' are acted
# on by the chat handler.
MEDIA_COMMANDS = {"/skip", "/cancel", "/status", "/rerun"}

# Every message the bot posts into the authoring chat starts with this, so
# the re-parse (which reads our own outgoing messages too) can skip them.
BOT_NOTICE_PREFIX = "🤖"

MEDIA_WAIT_TIMEOUT = 600.0  # seconds a scenario may wait for its media
STATE_FILE = "media_wait.json"

Say = Callable[[str], Awaitable[bool]]


def parse_placeholder(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """('photo'|'video', caption-or-None) for a placeholder line, else None."""
    match = PLACEHOLDER_RE.match(text)
    if match is None:
        return None
    kind = "video" if match.group(1).lower() in ("видео", "video") else "photo"
    return kind, (match.group(2).strip() or None)


def label(kind: str) -> str:
    return "видео" if kind == "video" else "фото"


def new_meta(header_msg) -> dict:
    return {
        "header_id": getattr(header_msg, "id", None),
        "header_date": getattr(header_msg, "date", None),
        "placeholders": [],  # [{"kind", "state": pending|filled|skipped, "entry"}]
        "wrong": [],  # [{"id", "expected"}] - media of the wrong type
        "cancelled": False,
    }


def first_pending(info: dict) -> Optional[dict]:
    for placeholder in info["placeholders"]:
        if placeholder["state"] == "pending":
            return placeholder
    return None


def is_unresolved(info: Optional[dict]) -> bool:
    """True if the scenario must NOT be activated yet (still missing media,
    or cancelled while waiting)."""
    return bool(info) and (info["cancelled"] or first_pending(info) is not None)


def media_kind(msg) -> Optional[str]:
    """'photo' / 'video' for an incoming media message, None for anything
    else (stickers, voice notes, documents, ...)."""
    if getattr(msg, "video", None) is not None:
        return "video"
    if getattr(msg, "photo", None) is not None:
        return "photo"
    mime = getattr(getattr(msg, "file", None), "mime_type", None) or ""
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "photo"
    return None


def finalize_scenario(turns: List[List[dict]], info: dict) -> None:
    """Once nothing is pending, drop the placeholders the user /skipped (and
    any turn that became empty because of it)."""
    skipped = [p["entry"] for p in info["placeholders"] if p["state"] == "skipped"]
    if not skipped:
        return
    for turn in turns:
        turn[:] = [entry for entry in turn if not any(entry is s for s in skipped)]
    turns[:] = [turn for turn in turns if turn]


# ---------------------------------------------------------------- state json

def load_state(state_dir: Path) -> dict:
    try:
        return json.loads((state_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_dir / STATE_FILE)


def set_last_activated(state_dir: Path, name: str) -> None:
    state = load_state(state_dir)
    state["last_activated"] = name
    save_state(state_dir, state)


def mark_completed(state_dir: Path) -> None:
    state = load_state(state_dir)
    state["completed"] = True
    save_state(state_dir, state)


# ------------------------------------------------------------- announcements

async def announce(
    say: Say, state_dir: Path, name: str, info: Optional[dict], now: Optional[datetime] = None
) -> Tuple[bool, Optional[float], bool]:
    """Tell the user what the bot is doing with the latest scenario, exactly
    once per event (the json remembers what was already said).

    Returns (blocked, recheck_in_seconds, just_completed):
      blocked        - don't activate this scenario now;
      recheck_in     - re-run the reload after this many seconds (timeout);
      just_completed - media is complete and this is the first time we see
                       it, so the caller should activate and say "Готово".
    """
    now = now or datetime.now(timezone.utc)
    state = load_state(state_dir)
    key = f"{name}:{info['header_id']}" if info else None

    still_waiting = state.get("waiting") and not state.get("ended") and not state.get("completed")
    if state.get("key") and state.get("key") != key and still_waiting:
        if await say(f"Сценарий «{state.get('name')}» отменён: пришёл новый сценарий."):
            state["waiting"] = False

    if not info or not info["placeholders"]:
        state.update(key=key, name=name, waiting=False, ended=False, completed=False)
        save_state(state_dir, state)
        return False, None, False

    if state.get("key") != key:
        state = {
            "last_activated": state.get("last_activated"),
            "key": key,
            "name": name,
            "waiting": True,
            "announced_wait": False,
            "progress": 0,
            "wrong": [],
            "ended": False,
            "completed": False,
        }

    placeholders = info["placeholders"]
    total = len(placeholders)
    done = sum(1 for p in placeholders if p["state"] != "pending")
    header_date = info.get("header_date")
    age = (now - header_date).total_seconds() if header_date else 0.0

    if state["ended"]:
        save_state(state_dir, state)
        return True, None, False

    if info["cancelled"]:
        if await say(f"Ожидание медиа отменено, сценарий «{name}» не запущен."):
            state["ended"] = True
        save_state(state_dir, state)
        return True, None, False

    if done < total and age > MEDIA_WAIT_TIMEOUT:
        minutes = int(MEDIA_WAIT_TIMEOUT // 60)
        if await say(f"Тайм-аут {minutes} минут: сценарий «{name}» отменён."):
            state["ended"] = True
        save_state(state_dir, state)
        return True, None, False

    if not state["announced_wait"]:
        order = " ".join(f"{i}) {label(p['kind'])}" for i, p in enumerate(placeholders, 1))
        text = f"Жду медиа: {order}. Пришли по порядку"
        untimed = [f"{i}) {label(p['kind'])}" for i, p in enumerate(placeholders, 1)
                   if "ttl" not in p["entry"]]
        if untimed:
            text += f"\n⚠ Без !timer (уйдут обычными, без самоуничтожения): {', '.join(untimed)}"
        if await say(text):
            state["announced_wait"] = True

    for wrong in info["wrong"]:
        if wrong["id"] not in state["wrong"]:
            if await say(f"Ожидалось {label(wrong['expected'])}, пришли ещё раз"):
                state["wrong"].append(wrong["id"])

    while state["progress"] < done:
        word = "Пропущено" if placeholders[state["progress"]]["state"] == "skipped" else "Принято"
        if not await say(f"{word} {state['progress'] + 1}/{total}"):
            break
        state["progress"] += 1

    if done < total:
        save_state(state_dir, state)
        return True, max(MEDIA_WAIT_TIMEOUT - age, 0.0) + 1.0, False

    just_completed = not state["completed"]
    state["waiting"] = False
    save_state(state_dir, state)
    return False, None, just_completed


def status_text(info: Optional[dict], active_name: Optional[str]) -> str:
    if info and first_pending(info) is not None and not info["cancelled"]:
        header_date = info.get("header_date")
        if header_date and (datetime.now(timezone.utc) - header_date).total_seconds() > MEDIA_WAIT_TIMEOUT:
            return "Время ожидания медиа истекло, сценарий отменён"
        placeholders = info["placeholders"]
        done = sum(1 for p in placeholders if p["state"] != "pending")
        missing = " ".join(
            f"{i}) {label(p['kind'])}"
            for i, p in enumerate(placeholders, 1)
            if p["state"] == "pending"
        )
        return f"Принято {done}/{len(placeholders)}. Не хватает: {missing}"
    if active_name:
        return f"Нет сценария, ожидающего медиа. Активный: «{active_name}»"
    return "Нет сценария, ожидающего медиа"
