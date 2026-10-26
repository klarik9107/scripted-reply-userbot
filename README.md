# scripted-reply-userbot

A Telegram **userbot** (built on [Telethon](https://docs.telethon.dev/), running
as a real user account — not the Bot API) that plays back a pre-written
conversation script, one "turn" of messages at a time, in reply to each
incoming message (text or media) from a specific person — in whichever chat
they message from, private or group. It simulates a read delay and a
"typing…" / "uploading…" indicator whose duration scales with message
length, so the pacing looks human.

> **⚠️ Intended use.** This tool is for **demonstration and staged/scripted
> video content only** — e.g. pre-recording a fake "live" conversation for a
> screen-capture video. Only run it against **your own secondary Telegram
> accounts** that you control on both ends of the conversation. Do not use it
> to impersonate real people, deceive third parties, or automate contact with
> accounts you do not own. You are responsible for complying with Telegram's
> Terms of Service and applicable law.

## How it works

1. You write a conversation script as a JSON file: a list of "turns", each
   turn being 1–3 messages (text and/or media) sent back-to-back.
2. You run the bot, pointed at a script and a target account.
3. Every time the target account sends *any* message (text, photo, voice
   note, sticker, whatever) in a chat with the bot's account, the bot waits
   a bit (simulated "read" delay), shows a typing/uploading indicator scaled
   to the reply's length, sends the next turn's message(s), and advances to
   the next turn. This works in a private chat or a group, whichever the
   target messages in.
4. Progress (which turn, and which message within that turn) is saved to
   disk after every single message sent — so restarting the process or a
   dropped connection mid-turn resumes from the next unsent message instead
   of replaying the whole turn or losing your place.
5. If the target fires off several messages back-to-back while a turn is
   still being sent, they're queued and handled strictly in order — nothing
   is dropped or processed out of sequence.
6. Sending the literal message `/reset_script` from the target account
   silently resets the script back to turn 0 (no visible reply, so the
   mechanic stays hidden on camera).

## 1. Get your API credentials

1. Go to <https://my.telegram.org> and log in with the phone number of the
   account that will run the bot (the "actor" account).
2. Open **API development tools** and create an application (any name/URL is
   fine — this is just for API access, not a public app).
3. Copy the **api_id** and **api_hash** shown there.

## 2. Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

- `API_ID`, `API_HASH` — from step 1.
- `SESSION_NAME` — any short name; Telethon will create `<SESSION_NAME>.session`
  next to the project after the first login.
- `TARGET_USERNAME_OR_ID` — the account whose messages should trigger replies
  (e.g. `@friend_username` or a numeric user id).
- The pacing variables (`READ_DELAY_*`, `TYPING_*`, `INTER_MESSAGE_DELAY_*`,
  `RECONNECT_*`) all have sensible defaults — tune them if you want faster or
  slower, more or less human-looking pacing.

## 4. First login (one-time, interactive)

The first time you run the bot with a given `SESSION_NAME`, Telethon needs to
log in interactively as your actor account:

```bash
python bot.py run --script scripts/demo.json
```

You'll be prompted in the terminal for:

- your phone number (international format, e.g. `+15551234567`),
- the login code Telegram sends to that account (via the Telegram app or SMS),
- your two-step-verification password, if you have one enabled.

After this, a `<SESSION_NAME>.session` file is saved locally and reused for
all future runs — no more prompts. **Keep this file private**; it is
equivalent to being logged into that Telegram account.

## 5. Write your own script

Create a new file under `scripts/`, e.g. `scripts/my_scene.json`:

```json
{
  "turns": [
    ["Hey! Long time no see"],
    ["I'm good, just been busy with work", "How about you?"],
    [{"media": "media/photo1.jpg", "caption": "check this out"}],
    ["That's great to hear!"]
  ]
}
```

- Each element of `turns` is one "turn": a list of **1 to 3** messages sent
  one after another (with a typing/uploading indicator + short pause before
  each).
- A message is either a plain string (text), or an object
  `{"media": "path/to/file", "caption": "optional text"}` to send a photo,
  video, voice note, or any other file, with an optional caption. The
  `media` path is resolved relative to the script file's own directory
  first (so `scripts/media/photo1.jpg` next to `scripts/my_scene.json`
  works as `"media/photo1.jpg"`), falling back to the project root.
- One turn is consumed per incoming message from the target account.

## 6. Run it

```bash
python bot.py run --script scripts/my_scene.json
```

(You can pass either `scripts/my_scene.json` or just `my_scene`.)

Leave this running in the chat you want to stage. Every message the target
account sends advances the script by one turn.

### List available scripts

```bash
python bot.py list-scripts
```

### Reset progress without touching Telegram

```bash
python bot.py reset --script my_scene
```

This is the same as sending `/reset_script` from the target account, but you
can do it from the command line without revealing anything in the chat.

### Preview a scenario without sending anything

```bash
python bot.py run --script my_scene --dry-run
```

Prints the whole timeline (who writes what, typing/read delays, `!notify`
lines, where a turn auto-continues) and exits — no Telegram connection.
`DRY_RUN=1` does the same.

## 7. Optional: auto-replies as "you" (`> text`)

A line starting with `> ` in a scenario is a reply sent **as your own
account** to Maria's chat, so the dialogue can run without you typing:

```
# example
hi
where have you been
!notify she's upset. say: sorry, I was lost in my thoughts
> sorry, I was lost in my thoughts
-
okay then
```

These lines don't count toward the 3-visible-message cap. If a turn **ends**
with a `> text` reply, the next turn starts automatically after the `-`;
if it ends with a normal message, the bot waits for your real reply as
before — so you can mix automatic and manual turns. At most 10 `> text`
replies are sent per scenario run.

Setup (one time):

1. Generate a session for your own account: `python make_me_session.py`
   (asks for phone, login code, and 2FA password if you have one).
2. Put the printed string into `.env` as `ME_SESSION_STRING`, and set
   `MARIA_PEER` to Maria's `@username` (or phone / numeric id).

If `ME_SESSION_STRING` is empty, `> text` lines are skipped with a warning
and the bot behaves as before.

> **Never commit `.env` or any `.session` file, and never share
> `ME_SESSION_STRING`.** They are equivalent to being logged in to those
> Telegram accounts. `.env` and `*.session` are already in `.gitignore`.

## Как присылать сценарий с медиа

Медиа не нужно класть в сценарий заранее. Сначала присылаешь текст сценария
в чат авторинга, а на месте фото/видео ставишь строку `<фото>` или `<видео>`
(после тега можно написать подпись). Дальше бот просит медиа, ты присылаешь
их следом, и бот подставляет по порядку.

```
# пример
привет
<фото> вот смотри
!timer 5
-
<видео> и видео
> ага
```

1. Бот отвечает: `🤖 Жду медиа: 1) фото 2) видео. Пришли по порядку`. Если у
   `<фото>`/`<видео>` нет `!timer`, он предупредит, но не заблокирует.
2. Присылаешь файлы по одному или альбомом (порядок по id сообщений). После
   каждого бот пишет `Принято 1/2`. Если тип не тот (ждали видео, пришло фото),
   пишет `Ожидалось видео, пришли ещё раз`, и счётчик не двигается.
3. Когда всё пришло: `Готово, сценарий «пример» запущен`. До этого момента
   сценарий **не** активируется, остаётся прежний.

Команды (писать в тот же чат):

| Команда | Что делает |
|---|---|
| `/status` | показывает, чего не хватает |
| `/skip` | пропускает текущий слот, он просто выпадает из сценария |
| `/cancel` | отменяет ожидание, сценарий не запускается |
| `/rerun` | запускает последний сценарий заново с начала, без повторной отправки медиа |

Если пришёл новый сценарий, пока старый ждёт медиа, старый отменяется с
уведомлением. Ожидание длится 10 минут, потом сценарий отменяется. Сообщения
бота начинаются с `🤖` и в сценарий не попадают. Файлы лежат в
`scripts/media/<имя сценария>/` и хранятся, поэтому после перезапуска бота
ничего присылать заново не нужно. Сценарии без `<фото>`/`<видео>` работают как
раньше (в том числе с фото, присланными прямо между строками).

## Logs

The bot logs to stdout via Python's standard `logging` module — which turn
was sent, to which chat, and when — for debugging. Nothing is ever posted to
the chat except the scripted messages themselves.

## Troubleshooting

- **"Telegram rejected API_ID/API_HASH as invalid"** — double-check the
  values copied from <https://my.telegram.org> against your `.env`.
- **"Session file exists but is not authorized"** — the session was revoked
  (e.g. you logged out that device from Telegram settings). Delete the
  `<SESSION_NAME>.session` file and run again to log in from scratch.
- **Connection drops** — the bot automatically reconnects (configurable via
  `RECONNECT_RETRIES` / `RECONNECT_DELAY` in `.env`); reconnect attempts and
  failures are logged.

## Project layout

```
scripted-reply-userbot/
  bot.py            # CLI entrypoint + Telethon event loop + playback logic
  config.py         # .env loading/validation
  state_store.py    # per-(session, script) progress persistence (JSON)
  scripts/
    demo.json        # example conversation script
  .env.example
  requirements.txt
  README.md
```
