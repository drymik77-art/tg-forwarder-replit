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
# Text cleaning & ad filter
# -------------------------
URL_PATTERN = re.compile(r"(https?://\S+|t\.me/\S+|\S+\.telegram\.me/\S+)")
MENTION_PATTERN = re.compile(r"@[\w_]+")

AD_KEYWORDS = [
    "підписуйся", "підписка", "підпишись", "на канал", "канал", "subscribe",
    "join", "follow", "promo", "співпраця", "реклама", "sale", "знижка", "shop"
]

def is_advertisement(text):
    """Перевіряє, чи схоже повідомлення на рекламу або платіжні реквізити."""
    if not text:
        return False
    lower = text.lower()

    # 🔹 1. Ключові слова реклами
    for k in AD_KEYWORDS:
        if k in lower:
            logging.info(f"🚫 Blocked ad post (keyword: '{k}')")
            return True

    # 🔹 2. Платіжні сервіси
    payment_keywords = [
        "monobank", "mono", "privatbank", "privat24", "paypal", "liqpay",
        "wise", "revolut", "binance", "bitcoin", "crypto", "usdt", "btc", "eth",
        "iban", "iban:", "card", "карта", "картка", "переказ", "рахунок", "донат", "donate"
    ]
    for k in payment_keywords:
        if k in lower:
            logging.info(f"🚫 Blocked payment-related post (keyword: '{k}')")
            return True

    # 🔹 3. Номери банківських карт
    if re.search(r"\b\d{16}\b", lower) or re.search(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b", lower):
        logging.info("🚫 Blocked post with possible card number")
        return True

    # 🔹 4. Забагато посилань
    if len(re.findall(r"https?://|t\.me/", lower)) >= 2:
        logging.info("🚫 Blocked post with multiple links")
        return True

    return False

def clean_text(text):
    """Очищує повідомлення: видаляє посилання, згадки, підписи."""
    if not text:
        return text

    text = URL_PATTERN.sub("", text)
    text = MENTION_PATTERN.sub("", text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = text.strip()

    # Видаляємо короткі підписи типу "Україна Live" або "Новини 24" чи з "підписатися"
    lines = text.split("\n")
    if len(lines) > 1:
        last_line = lines[-1].strip().lower()
        if (
            len(last_line.split()) <= 5 and
            (not re.search(r"[.!?,:;]", last_line) or "підписатися" in last_line)
        ):
            logging.info(f"🧹 Removed possible channel signature: '{lines[-1]}'")
            lines = lines[:-1]
            text = "\n".join(lines).strip()

    return text

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
# Active hours
# -------------------------
def is_active_now():
    now = datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)
    h = now.hour
    if start_hour <= end_hour:
        return start_hour <= h < end_hour
    else:
        return h >= start_hour or h < end_hour

# -------------------------
# Message forwarding
# -------------------------
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

        # Якщо повідомлення є частиною альбому
        if msg.grouped_id:
            grouped_id = msg.grouped_id
            # Отримуємо всі повідомлення з тієї ж групи
            entity = await client.get_entity(chat_id)
            grouped = []
            async for m in client.iter_messages(entity, limit=10):
                if m.grouped_id == grouped_id:
                    grouped.append(m)
            grouped = sorted(grouped, key=lambda x: x.id)  # порядок правильний

            # Використовуємо текст лише з першого повідомлення
            main_msg = grouped[0]
            text_clean, _ = strip_entities(main_msg)
            if is_advertisement(text_clean):
                logging.info(f"🚫 Skipped ad/payment album from {chat_id}:{msg.id}")
                return

            # Збираємо медіа-файли
            media_files = [m.media for m in grouped if m.media]

            if text_clean and len(text_clean) > 1024:
                text_clean = text_clean[:1021] + "..."

            await client.send_file(TARGET_CHANNEL, media_files, caption=text_clean or None)
            for m in grouped:
                mark_processed(chat_id, m.id)
            logging.info(f"✅ Forwarded album {chat_id}:{grouped_id} ({len(media_files)} files)")
            return

        # 🔎 Якщо не альбом — стандартна логіка
        text_clean, _ = strip_entities(msg)
        if is_advertisement(text_clean):
            logging.info(f"🚫 Skipped ad/payment message from {chat_id}:{msg.id}")
            return

        if text_clean and len(text_clean) > 1024:
            text_clean = text_clean[:1021] + "..."

        if msg.media:
            await client.send_file(TARGET_CHANNEL, msg.media, caption=text_clean or None)
        elif text_clean:
            await client.send_message(TARGET_CHANNEL, text_clean)
        else:
            return

        mark_processed(chat_id, msg_id)
        logging.info(f"✅ Forwarded message {chat_id}:{msg_id}")

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
                    async for msg in client.iter_messages(entity, limit=1):
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
# Instant event handler
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
