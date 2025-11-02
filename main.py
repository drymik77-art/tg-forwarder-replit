# main.py
import os
import re
import logging
import sqlite3
import threading
import asyncio
from datetime import datetime, timedelta
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.tl.types import (
    MessageEntityUrl, MessageEntityTextUrl,
    MessageEntityMention, MessageEntityMentionName
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# -------------------------
# Environment variables (set these in Replit Secrets)
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

# Convert source channels (support both @name and numeric IDs)
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
# Database for processed messages
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
    if not text:
        return text
    text = URL_PATTERN.sub("", text)
    text = MENTION_PATTERN.sub("", text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()

def strip_entities(message):
    text = message.message or ""
    if not text:
        return text, None
    if not hasattr(message, 'entities') or not message.entities:
        return clean_text(text), None
    chars = list(text)
    for ent in message.entities:
        if isinstance(ent, (MessageEntityUrl, MessageEntityTextUrl, MessageEntityMention, MessageEntityMentionName)):
            for i in range(ent.offset, ent.offset + ent.length):
                if 0 <= i < len(chars):
                    chars[i] = ''
    filtered = ''.join(chars)
    return clean_text(filtered), None

# -------------------------
# Active hours check
# -------------------------
def is_active_now():
    now = datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)
    h = now.hour
    if start_hour <= end_hour:
        return start_hour <= h < end_hour
    else:
        return h >= start_hour or h < end_hour

# -------------------------
# Handler for new messages
# -------------------------
@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def handler(event):
    try:
        m = event.message
        chat_id = event.chat_id
        msg_id = m.id

        if is_processed(chat_id, msg_id):
            return

        if not is_active_now():
            logging.info("Outside active hours; skipping message %s:%s", chat_id, msg_id)
            return

        text_clean, _ = strip_entities(m)

        if m.media:
            caption = text_clean if text_clean else None
            await client.send_file(TARGET_CHANNEL, m.media, caption=caption)
            logging.info("Media sent from %s:%s to %s", chat_id, msg_id, TARGET_CHANNEL)
        elif text_clean:
            await client.send_message(TARGET_CHANNEL, text_clean)
            logging.info("Text sent from %s:%s to %s", chat_id, msg_id, TARGET_CHANNEL)
        else:
            logging.info("Message %s:%s had no content after cleaning; skipped", chat_id, msg_id)

        mark_processed(chat_id, msg_id)

    except Exception as e:
        logging.exception("Error processing message: %s", e)

# -------------------------
# Debug visibility
# -------------------------
@client.on(events.NewMessage())
async def debug_all(event):
    try:
        chat = await event.get_chat()
        chat_name = getattr(chat, 'title', None) or getattr(chat, 'username', None) or str(event.chat_id)
        logging.info(f"📨 DEBUG: New message detected from {chat_name} (ID: {event.chat_id})")
    except Exception as e:
        logging.warning(f"⚠️ Debug error while resolving chat info: {e}")

# -------------------------
# Start bot
# -------------------------
def run_telethon():
    async def start_and_run():
        init_db()
        await client.start()
        logging.info("✅ Connected to Telegram API")

        # Try to preload all channels
        for src in SOURCE_CHANNELS:
            try:
                if isinstance(src, int):
                    await asyncio.sleep(1)
                    entity_id = get_peer_id(src)
                    await client.get_entity(entity_id)
                else:
                    await client.get_entity(src)
                logging.info(f"✅ Loaded entity for {src}")
            except Exception as e:
                logging.warning(f"⚠️ Could not load entity for {src}: {e}")
                await asyncio.sleep(3)
                try:
                    await client.get_entity(src)
                    logging.info(f"✅ Retried and loaded entity for {src}")
                except Exception as e2:
                    logging.error(f"❌ Failed to load entity for {src}: {e2}")

        # List dialogs for debugging
        async for d in client.iter_dialogs():
            logging.info(f"📋 Dialog visible: {d.name} — {d.id}")

        logging.info(f"🟢 Telethon client started. Sources: {SOURCE_CHANNELS} | Target: {TARGET_CHANNEL}")
        await client.run_until_disconnected()

    try:
        asyncio.run(start_and_run())
    except Exception as e:
        logging.exception("Telethon runner stopped: %s", e)

# -------------------------
# Flask app
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
