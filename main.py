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
    MessageEntityUrl, MessageEntityTextUrl,
    MessageEntityMention, MessageEntityMentionName
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# -------------------------
# Environment variables
# -------------------------
def getenv_required(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v

SESSION_STRING = getenv_required("SESSION_STRING")
API_ID = int(getenv_required("API_ID"))
API_HASH = getenv_required("API_HASH")
SOURCE_CHANNELS_RAW = os.environ.get("SOURCE_CHANNELS", "")
TARGET_CHANNEL = getenv_required("TARGET_CHANNEL")
ACTIVE_HOURS_RAW = os.environ.get("ACTIVE_HOURS", "0,24")
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "0"))
DB_PATH = os.environ.get("DB_PATH", "processed.db")
PORT = int(os.environ.get("PORT", os.environ.get("REPL_PORT", 8080)))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))

SOURCE_CHANNELS = []
for s in SOURCE_CHANNELS_RAW.split(","):
    s = s.strip()
    if not s:
        continue
    if s.startswith("-100") or s.isdigit():
        SOURCE_CHANNELS.append(int(s))
    else:
        SOURCE_CHANNELS.append(s)

try:
    start_hour, end_hour = (int(x.strip()) for x in ACTIVE_HOURS_RAW.split(","))
except Exception:
    start_hour, end_hour = 0, 24

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# -------------------------
# Database
# -------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS processed (
        chat_id TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        PRIMARY KEY (chat_id, message_id)
    )
    """)
    conn.commit()
    conn.close()

def is_processed(chat_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed WHERE chat_id=? AND message_id=?", (str(chat_id), int(message_id)))
    res = cur.fetchone()
    conn.close()
    return res is not None

def mark_processed(chat_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO processed (chat_id, message_id) VALUES (?, ?)", (str(chat_id), int(message_id)))
    conn.commit()
    conn.close()

# -------------------------
# Text cleaning
# -------------------------
URL_PATTERN = re.compile(r"(https?://\S+|t\.me/\S+|\S+\.telegram\.me/\S+)")
MENTION_PATTERN = re.compile(r"@[\w_]+")

def clean_text(text):
    """Прибирає посилання, зайві пробіли та порожні рядки."""
    if not text:
        return text
    text = URL_PATTERN.sub("", text)
    text = MENTION_PATTERN.sub("", text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()

def strip_entities(message):
    """Видаляє лише посилання, але залишає текст слів, до яких вони були прикріплені."""
    text = message.message or ""
    if not text:
        return text, None

    if not hasattr(message, "entities") or not message.entities:
        return clean_text(text), None

    chars = list(text)
    for ent in message.entities:
        # Якщо це текст із вбудованим посиланням — залишаємо слово
        if isinstance(ent, MessageEntityTextUrl):
            continue
        # Якщо це звичайне посилання або згадка — видаляємо
        if isinstance(ent, (MessageEntityUrl, MessageEntityMention, MessageEntityMentionName)):
            for i in range(ent.offset, ent.offset + ent.length):
                if 0 <= i < len(chars):
                    chars[i] = ''
    filtered = ''.join(chars)
    return clean_text(filtered), None

# -------------------------
# Active hours
# -------------------------
def is_active_now():
    now = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)
    h = now.hour
    if start_hour <= end_hour:
        return start_hour <= h < end_hour
    else:
        return h >= start_hour or h < end_hour

# -------------------------
# Album buffer (оновлена логіка)
# -------------------------
album_buffer = defaultdict(list)
album_timers = {}

async def forward_album(messages, chat_id):
    try:
        if not is_active_now():
            logging.info("Outside active hours; skipping album %s", chat_id)
            return

        # Сортуємо медіа за ID для збереження порядку
        messages = sorted(messages, key=lambda m: m.id)
        media_files = []
        caption = None

        for m in messages:
            if m.media:
                media_files.append(m.media)
            if not caption and m.message:
                caption = clean_text(m.message)

        if caption and len(caption) > 1024:
            caption = caption[:1021] + "..."

        await client.send_file(TARGET_CHANNEL, media_files, caption=caption)
        logging.info(f"📸 Forwarded album ({len(media_files)} files) from {chat_id}")

        for m in messages:
            mark_processed(chat_id, m.id)

    except Exception as e:
        logging.exception(f"Error forwarding album from {chat_id}: {e}")

# -------------------------
# Message forwarding
# -------------------------
async def forward_message(msg, chat_id):
    try:
        msg_id = msg.id
        if is_processed(chat_id, msg_id):
            return

        if not is_active_now():
            logging.info("Outside active hours; skipping message %s:%s", chat_id, msg_id)
            return

        # 🚫 Ігноруємо повідомлення з кнопками
        if hasattr(msg, "buttons") and msg.buttons:
            logging.info(f"🚫 Skipped message {chat_id}:{msg.id} — contains inline buttons (possible ad)")
            mark_processed(chat_id, msg.id)
            return

        # 🎞 Якщо це частина альбому
        if msg.grouped_id:
            album_buffer[msg.grouped_id].append(msg)

            # Якщо є старий таймер — скасовуємо
            if msg.grouped_id in album_timers:
                album_timers[msg.grouped_id].cancel()

            async def flush_album():
                group = album_buffer.pop(msg.grouped_id, [])
                if group:
                    await forward_album(group, chat_id)

            # чекаємо 3 секунди після останнього файлу альбому
            loop = asyncio.get_event_loop()
            album_timers[msg.grouped_id] = loop.call_later(3, lambda: asyncio.create_task(flush_album()))
            return

        # 📝 Звичайне повідомлення
        text_clean, _ = strip_entities(msg)

        if text_clean and len(text_clean) > 1024:
            logging.warning(f"✂️ Caption too long ({len(text_clean)} chars). Truncating...")
            text_clean = text_clean[:1021] + "..."

        if msg.media:
            caption = text_clean if text_clean else None
            await client.send_file(TARGET_CHANNEL, msg.media, caption=caption)
        elif text_clean:
            await client.send_message(TARGET_CHANNEL, text_clean)
        else:
            return

        logging.info(f"✅ Forwarded message {chat_id}:{msg_id}")
        mark_processed(chat_id, msg_id)

    except Exception as e:
        logging.exception(f"Error forwarding {chat_id}:{msg.id}: {e}")

# -------------------------
# Poller
# -------------------------
async def poll_channels():
    while True:
        try:
            for src in SOURCE_CHANNELS:
                try:
                    entity = await client.get_entity(src)
                    async for msg in client.iter_messages(entity, limit=3):
                        if not is_processed(msg.chat_id, msg.id):
                            await forward_message(msg, msg.chat_id)
                except Exception as e:
                    logging.warning(f"⚠️ Poller failed for {src}: {e}")
            logging.info(f"⏱ Poll cycle complete. Sleeping {POLL_INTERVAL} seconds...")
            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logging.error(f"Poller loop error: {e}")
            await asyncio.sleep(60)

# -------------------------
# Event handler
# -------------------------
@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def handler(event):
    await forward_message(event.message, event.chat_id)

# -------------------------
# Start bot
# -------------------------
def run_telethon():
    async def start_and_run():
        init_db()
        await client.start()
        logging.info("✅ Connected to Telegram API")

        for src in SOURCE_CHANNELS:
            try:
                await client.get_entity(src)
                logging.info(f"✅ Loaded entity for {src}")
            except Exception as e:
                logging.warning(f"⚠️ Could not load entity for {src}: {e}")

        asyncio.create_task(poll_channels())
        await client.run_until_disconnected()

    asyncio.run(start_and_run())

# -------------------------
# Flask
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "OK - bot is alive", 200

def start_flask():
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    logging.info(f"Starting bot: launching Telethon in background thread and Flask server (port {PORT})")
    t = threading.Thread(target=run_telethon, daemon=True)
    t.start()
    start_flask()
