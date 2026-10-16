#!/usr/bin/env python3
"""One-off helper: interactively log in to YOUR OWN Telegram account (the
account that will send '> text' auto-replies to Maria) and print a
Telethon StringSession for the ME_SESSION_STRING setting in .env.

This script never writes the session to disk and never logs it anywhere -
it only prints it once to the console at the end. Copy it straight into
.env and never share it or commit it: it is equivalent to full access to
that Telegram account (same as a .session file).

Usage:
    python make_me_session.py

You'll be prompted for your phone number, the login code Telegram sends
you, and your two-step-verification password if you have one enabled -
exactly like the normal first-run login for the other sessions in this
project (see README.md).
"""

from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import load_config


async def main() -> None:
    cfg = load_config()
    async with TelegramClient(StringSession(), cfg.api_id, cfg.api_hash) as client:
        me = await client.get_me()
        session_string = client.session.save()

    print()
    print(f"Logged in as: {me.first_name or ''} {me.last_name or ''} (@{me.username}) id={me.id}")
    print()
    print("Your ME_SESSION_STRING - copy this exact line into .env and keep it secret:")
    print()
    print(session_string)
    print()


if __name__ == "__main__":
    asyncio.run(main())
