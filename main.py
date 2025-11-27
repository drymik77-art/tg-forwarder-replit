# main.py
import os
import re
import logging
import sqlite3
import threading
import asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageEntityUrl,
    MessageEntityTextUrl,
    MessageEntityMention,
    MessageEntityMentionName,
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# -------------------------
# Environment variables
# -------------------------
def getenv_required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v

SESSION_STRING = getenv_required("SESSION_STRING")
API_ID = int(getenv_required("API_ID"))
API_HASH = getenv_required("API_HASH")
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "0"))
DB_PATH = os.environ.get("DB_PATH", "processed.db")
PORT = int(os.environ.get("PORT", os.environ.get("REPL_PORT", 8080)))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))

# -------------------------
# NEW: Day/Night mode variables
# -------------------------
def parse_channels(raw: str):
    arr = []
    for s in raw.split(","):
        s = s.strip()
        if not s:
            continue
        if s.startswith("-100") or s.isdigit():
            arr.append(int(s))
        else:
            arr.append(s)
    return arr

NIGHT_SOURCE_CHANNELS = parse_channels(os.environ.get("NIGHT_SOURCE_CHANNELS", ""))
DAY_SOURCE_CHANNELS   = parse_channels(os.environ.get("DAY_SOURCE_CHANNELS", ""))

NIGHT_TARGET_CHANNEL  = os.environ.get("NIGHT_TARGET_CHANNEL", "")
DAY_TARGET_CHANNEL    = os.environ.get("DAY_TARGET_CHANNEL", "")

def parse_hours(raw):
    """
    Парсить формати:
    - "22,7"                 (старий, тільки години)
    - "22:45,07:15"          (новий, години + хвилини)
    - "07:5,22:5"            (автоматично стане 07:05 → 22:05)
    Повертає (start_minutes, end_minutes)
    """
    try:
        start_raw, end_raw = raw.split(",")

        def to_minutes(x):
            x = x.strip()
            if ":" in x:
                h, m = x.split(":")
                return int(h) * 60 + int(m)
            return int(x) * 60  # якщо тільки година, хвилина = 00

        start_min = to_minutes(start_raw)
        end_min = to_minutes(end_raw)

        return start_min, end_min

    except Exception:
        # Якщо помилка → працюємо 24/7
        return 0, 24 * 60


# Завантаження годин із Railway Variables
NIGHT_HOURS = parse_hours(os.environ.get("NIGHT_HOURS", "22:45,07:15"))
DAY_HOURS   = parse_hours(os.environ.get("DAY_HOURS",   "07:45,22:15"))


def in_range_minutes(now_min, start_min, end_min):
    """
    Перевіряє, чи час now_min входить у діапазон,
    підтримує режим “через північ” (наприклад, 22:45 → 07:15).
    """
    if start_min <= end_min:
        return start_min <= now_min < end_min
    else:
        return now_min >= start_min or now_min < end_min


def get_current_mode():
    """
    Повертає "night", "day" або None.
    """
    now = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)
    now_min = now.hour * 60 + now.minute

    night_start, night_end = NIGHT_HOURS
    day_start, day_end = DAY_HOURS

    if in_range_minutes(now_min, night_start, night_end):
        return "night"

    if in_range_minutes(now_min, day_start, day_end):
        return "day"

    return None


# -------------------------
# Telethon client
# -------------------------
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# -------------------------
# Database
# -------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS processed (
            chat_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        )
        """
    )
    conn.commit()
    conn.close()

def is_processed(chat_id, message_id) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed WHERE chat_id=? AND message_id=?",
                (str(chat_id), int(message_id)))
    res = cur.fetchone()
    conn.close()
    return res is not None

def mark_processed(chat_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO processed (chat_id, message_id) VALUES (?, ?)",
        (str(chat_id), int(message_id)),
    )
    conn.commit()
    conn.close()

# -------------------------
# Cleaning text (entities, emojis)
# -------------------------
def clean_text(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()

def expand_word(text: str, start: int, end: int) -> tuple[int, int]:
    left = start
    while left > 0 and not text[left - 1].isspace():
        left -= 1
    right = end
    while right < len(text) and not text[right].isspace():
        right += 1
    return left, right

def strip_entities(message):
    text = message.message or ""
    if not text:
        return text, None

    chars = list(text)
    n = len(chars)

    utf16 = text.encode("utf-16-le")

    def utf16_to_py(i: int) -> int:
        return len(utf16[: i * 2].decode("utf-16-le", errors="ignore"))

    to_remove = []

    for ent in getattr(message, "entities", []) or []:
        start = utf16_to_py(ent.offset)
        end   = utf16_to_py(ent.offset + ent.length)

        start = max(0, min(start, n))
        end   = max(0, min(end, n))

        entity_text = text[start:end]

        if "t.me" in entity_text or "telegram.me" in entity_text:
            s, e = expand_word(text, start, end)
            to_remove.append((s, e))
            continue

        if isinstance(ent, (MessageEntityMention, MessageEntityMentionName)):
            s, e = expand_word(text, start, end)
            to_remove.append((s, e))
            continue

        if isinstance(ent, MessageEntityUrl):
            to_remove.append((start, end))
            continue

        if isinstance(ent, MessageEntityTextUrl):
            if "t.me" in ent.url or "telegram.me" in ent.url:
                s, e = expand_word(text, start, end)
                to_remove.append((s, e))
            continue

    for s, e in to_remove:
        for i in range(s, e):
            chars[i] = ""

    cleaned = "".join(chars)
    cleaned = clean_text(cleaned)
    return cleaned, None

EMOJI_PATTERN = re.compile(
    "[" 
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]+"
)

def remove_emojis(text: str) -> str:
    if not text:
        return text
    return EMOJI_PATTERN.sub("", text).strip()

def clean_message_text(msg) -> str:
    text, _ = strip_entities(msg)
    text = remove_emojis(text)
    return text

# -------------------------
# Filters (fraud, casino, donate)
# -------------------------
CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

BLOCK_WORDS = [
    "збір коштів", "проводимо збір", "закрити збір", "casino",
    "казино", "виграш", "реклама", "розіграш", "донат"
]

CASINO_URL_PATTERN = re.compile(r"(1xbet|bet|casino|slot|win)", flags=re.IGNORECASE)

DONATE_URL_PATTERN = re.compile(
    r"(mono\.me|send\.monobank\.ua|paypal\.me|buymeacoffee\.com)",
    flags=re.IGNORECASE,
)

def is_blocked_content(text: str):
    if not text:
        return None

    t = text.lower()

    if CARD_PATTERN.search(text):
        return "card number detected"

    for w in BLOCK_WORDS:
        if w in t:
            return f"blocked word: {w}"

    if CASINO_URL_PATTERN.search(t):
        return "casino/stake link"

    if DONATE_URL_PATTERN.search(t):
        return "donation link"

    return None

# -------------------------
# Album buffer
# -------------------------
album_buffer = defaultdict(list)
album_timers = {}

async def forward_album(messages, chat_id):
    try:
        mode = get_current_mode()
        if mode == "night":
            target = NIGHT_TARGET_CHANNEL
        elif mode == "day":
            target = DAY_TARGET_CHANNEL
        else:
            return

        messages = sorted(messages, key=lambda m: m.id)
        media_files = []

        caption_raw = None
        first_msg = None

        for m in messages:
            if m.media:
                media_files.append(m.media)
            if not caption_raw and m.message:
                caption_raw = m.message
                first_msg = m

        caption = None
        if caption_raw:
            reason = is_blocked_content(caption_raw)
            if reason:
                for m in messages:
                    mark_processed(chat_id, m.id)
                return

            caption_clean = clean_message_text(first_msg)

            reason = is_blocked_content(caption_clean)
            if reason:
                for m in messages:
                    mark_processed(chat_id, m.id)
                return

            if len(caption_clean) > 1024:
                caption_clean = caption_clean[:1021] + "..."

            caption = caption_clean

        await client.send_file(target, media_files, caption=caption)

        for m in messages:
            mark_processed(chat_id, m.id)

    except Exception as e:
        logging.error(f"Album error: {e}")

# -------------------------
# Forward single message
# -------------------------
async def forward_message(msg, chat_id):
    try:
        if is_processed(chat_id, msg.id):
            return

        mode = get_current_mode()
        if mode == "night":
            target = NIGHT_TARGET_CHANNEL
        elif mode == "day":
            target = DAY_TARGET_CHANNEL
        else:
            return

        if hasattr(msg, "buttons") and msg.buttons:
            mark_processed(chat_id, msg.id)
            return

        # Album grouping
        if msg.grouped_id:
            album_buffer[msg.grouped_id].append(msg)
            if msg.grouped_id in album_timers:
                album_timers[msg.grouped_id].cancel()

            async def flush():
                group = album_buffer.pop(msg.grouped_id, [])
                if group:
                    await forward_album(group, chat_id)

            loop = asyncio.get_event_loop()
            album_timers[msg.grouped_id] = loop.call_later(
                3, lambda: asyncio.create_task(flush())
            )
            return

        # Raw filter
        reason = is_blocked_content(msg.message or "")
        if reason:
            mark_processed(chat_id, msg.id)
            return

        text_clean = clean_message_text(msg)

        reason = is_blocked_content(text_clean)
        if reason:
            mark_processed(chat_id, msg.id)
            return

        if text_clean and len(text_clean) > 1024:
            text_clean = text_clean[:1021] + "..."

        if msg.media:
            if isinstance(msg.media, MessageMediaWebPage):
                if text_clean:
                    await client.send_message(target, text_clean)
            elif isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)):
                caption = text_clean if text_clean else None
                await client.send_file(target, msg.media, caption=caption)
                logging.info(f"📸 Sent MEDIA → {target}")
            else:
                if text_clean:
                    await client.send_message(target, text_clean)
        else:
            if text_clean:
                await client.send_message(target, text_clean)
                logging.info(f"📨 Sent TEXT → {target}")

        mark_processed(chat_id, msg.id)

    except Exception as e:
        logging.error(f"Forward error: {e}")

# -------------------------
# Polling
# -------------------------
async def poll_channels():
    while True:
        try:
            mode = get_current_mode()
            if mode == "night":
                active_sources = NIGHT_SOURCE_CHANNELS
            elif mode == "day":
                active_sources = DAY_SOURCE_CHANNELS
            else:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            for src in active_sources:
                try:
                    entity = await client.get_entity(src)
                    async for msg in client.iter_messages(entity, limit=5):
                        if not is_processed(msg.chat_id, msg.id):
                            await forward_message(msg, msg.chat_id)
                except Exception as e:
                    logging.warning(f"Poll issue for {src}: {e}")

            await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logging.error(f"Poll loop error: {e}")
            await asyncio.sleep(60)

# -------------------------
# Event handler (live updates)
# -------------------------
@client.on(events.NewMessage())
async def handler(event):
    msg = event.message
    chat = event.chat_id

    mode = get_current_mode()
    if mode == "night":
        if chat not in NIGHT_SOURCE_CHANNELS:
            return
    elif mode == "day":
        if chat not in DAY_SOURCE_CHANNELS:
            return
    else:
        return

    await forward_message(msg, chat)

# -------------------------
# Startup
# -------------------------
def run_telethon():
    async def start_and_run():
        init_db()
        await client.start()

        logging.info("✅ Telegram connected.")

        # -------------------------
        # Load channels info at startup
        # -------------------------
        logging.info("🔌 Loading channels for both modes...")

        # --- NIGHT MODE CHANNELS ---
        logging.info("🌙 Night mode channels:")
        for src in NIGHT_SOURCE_CHANNELS:
            try:
                entity = await client.get_entity(src)
                title = getattr(entity, "title", None)
                logging.info(f"   ✓ NIGHT source {src} ({title})")
            except Exception as e:
                logging.warning(f"   ⚠️ Could not load NIGHT entity {src}: {e}")

        logging.info(f"   🎯 NIGHT target channel: {NIGHT_TARGET_CHANNEL}")

        # --- DAY MODE CHANNELS ---
        logging.info("☀️ Day mode channels:")
        for src in DAY_SOURCE_CHANNELS:
            try:
                entity = await client.get_entity(src)
                title = getattr(entity, "title", None)
                logging.info(f"   ✓ DAY source {src} ({title})")
            except Exception as e:
                logging.warning(f"   ⚠️ Could not load DAY entity {src}: {e}")

        logging.info(f"   🎯 DAY target channel: {DAY_TARGET_CHANNEL}")

        # Poller interval log
        logging.info(f"⏱ Poller interval set to {POLL_INTERVAL} seconds")

        logging.info("🚀 Bot is fully initialized and listening for messages.")

        # Start poller
        asyncio.create_task(poll_channels())

        await client.run_until_disconnected()

    asyncio.run(start_and_run())


# -------------------------
# Flask
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "OK - bot alive", 200

def start_flask():
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    t = threading.Thread(target=run_telethon, daemon=True)
    t.start()
    start_flask()
