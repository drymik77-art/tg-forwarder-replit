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
    MessageEntityMention, MessageEntityMentionName,
    MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage
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

URL_PATTERN = re.compile(r"https?://\S+|t\.me/\S+|\S+\.telegram\.me/\S+")
MENTION_PATTERN = re.compile(r"@[\w_]+")

def clean_text(text):
    if not text:
        return text
    text = URL_PATTERN.sub("", text)
    text = MENTION_PATTERN.sub("", text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()

def strip_entities(message):
    text = message.message or ""
    if not text:
        return text, None

    if not hasattr(message, "entities") or not message.entities:
        return clean_text(text), None

    chars = list(text)
    n = len(chars)

    def is_telegram_url(url: str) -> bool:
        return "t.me/" in url or "telegram.me/" in url

    def is_word_boundary(ch: str) -> bool:
        # роздільники слів: пробіли, переноси, розділові знаки, вертикальна риска тощо
        return ch.isspace() or ch in ",.!?;:()[]{}«»\"'|"

    for ent in message.entities:
        # 1) Текст із вбудованим посиланням
        if isinstance(ent, MessageEntityTextUrl):
            if not is_telegram_url(ent.url):
                # зовнішній URL → залишаємо слово, нічого не вирізаємо
                continue

            # для Telegram-лінків — видаляємо ВСЕ слово цілком
            start = ent.offset
            end = ent.offset + ent.length

            # розширюємо вліво до межі слова
            while start > 0 and not is_word_boundary(chars[start - 1]):
                start -= 1

            # розширюємо вправо до межі слова
            while end < n and not is_word_boundary(chars[end]):
                end += 1

            for i in range(start, end):
                chars[i] = ""

        # 2) Звичайний URL (просто посилання)
        elif isinstance(ent, MessageEntityUrl):
            start = ent.offset
            end = ent.offset + ent.length
            for i in range(start, end):
                if 0 <= i < n:
                    chars[i] = ""

        # 3) @username та згадки за ID
        elif isinstance(ent, (MessageEntityMention, MessageEntityMentionName)):
            start = ent.offset
            end = ent.offset + ent.length
            for i in range(start, end):
                if 0 <= i < n:
                    chars[i] = ""

    filtered = "".join(chars)
    return clean_text(filtered), None

# -------------------------
# Content filters (NEW)
# -------------------------

CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

BLOCK_WORDS = ["збір коштів", "проводимо збір", "casino", "казино", "виграш", "реклама", "розіграш", "розігруємо", "донат", "промо"]

CASINO_URL_PATTERN = re.compile(
    r"(1xbet|bet|casino|ggbet|parimatch|slot|win)",
    flags=re.IGNORECASE
)

DONATE_URL_PATTERN = re.compile(
    r"(mono\.me|send\.monobank\.ua|paypal\.me|buymeacoffee\.com)",
    flags=re.IGNORECASE
)

# видалення емодзі
EMOJI_PATTERN = re.compile(
    "["                
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map symbols
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"          # misc symbols
    "\u2700-\u27BF"          # dingbats
    "]+",
    flags=re.UNICODE
)

def remove_emojis(text: str) -> str:
    if not text:
        return text
    return EMOJI_PATTERN.sub("", text).strip()


def is_blocked_content(text: str):
    """
    Повертає рядок з причиною блокування або None, якщо все ок.
    """
    if not text:
        return None

    lower = text.lower()

    # 1️⃣ Банківська картка
    if CARD_PATTERN.search(text):
        return "знайдено схожий на номер банківської картки фрагмент"

    # 2️⃣ Заборонені слова
    for w in BLOCK_WORDS:
        if w in lower:
            return f"знайдено заборонене слово '{w}'"

    # 3️⃣ Казино / ставки
    if CASINO_URL_PATTERN.search(lower):
        return "знайдено згадку/посилання на казино або ставки"

    # 4️⃣ Збір коштів
    if DONATE_URL_PATTERN.search(lower):
        return "знайдено посилання на збір коштів"

    return None



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
# Album buffer
# -------------------------
album_buffer = defaultdict(list)
album_timers = {}

async def forward_album(messages, chat_id):
    try:
        if not is_active_now():
            logging.info("Outside active hours; skipping album %s", chat_id)
            return

        messages = sorted(messages, key=lambda m: m.id)
        media_files = []
        
        # -------------------------------
        #   NEW CAPTION + FILTER LOGIC
        # -------------------------------
        # шукаємо перше повідомлення з текстом
        caption_raw = None
        first_msg = None

        for m in messages:
            if m.media:
                media_files.append(m.media)

            if not caption_raw and m.message:
                caption_raw = m.message
                first_msg = m

        # якщо є текст — перевіряємо фільтри
        caption = None
        if caption_raw:

            # 1) фільтр по сирому тексту
            reason = is_blocked_content(caption_raw)
            if reason:
                logging.info(f"🚫 Blocked album {chat_id} — {reason}")
                for m in messages:
                    mark_processed(chat_id, m.id)
                return

            # 2) видаляємо entities + emojis
            caption_clean, _ = strip_entities(first_msg)
            caption_clean = remove_emojis(caption_clean)

            # 3) фільтр після чистки
            reason = is_blocked_content(caption_clean)
            if reason:
                logging.info(f"🚫 Blocked cleaned album {chat_id} — {reason}")
                for m in messages:
                    mark_processed(chat_id, m.id)
                return

            if len(caption_clean) > 1024:
                caption_clean = caption_clean[:1021] + "..."

            caption = caption_clean
        else:
            caption = None

        await client.send_file(TARGET_CHANNEL, media_files, caption=caption)
        logging.info(f"📸 Forwarded album ({len(media_files)} files) from {chat_id}")

        for m in messages:
            mark_processed(chat_id, m.id)

    except Exception as e:
        logging.exception(f"Error forwarding album: {e}")

# -------------------------
# Message forwarding
# -------------------------
async def forward_message(msg, chat_id):
    try:
        if is_processed(chat_id, msg.id):
            return

        if not is_active_now():
            return

        if hasattr(msg, "buttons") and msg.buttons:
            logging.info(f"🚫 Blocked {chat_id}:{msg.id} — повідомлення містить кнопки")
            mark_processed(chat_id, msg.id)
            return

        if msg.grouped_id:
            album_buffer[msg.grouped_id].append(msg)
            if msg.grouped_id in album_timers:
                album_timers[msg.grouped_id].cancel()

            async def flush_album():
                group = album_buffer.pop(msg.grouped_id, [])
                if group:
                    await forward_album(group, chat_id)

            loop = asyncio.get_event_loop()
            album_timers[msg.grouped_id] = loop.call_later(
                3, lambda: asyncio.create_task(flush_album())
            )
            return

        # --------------------------  
        # ★ НОВИЙ БЛОК — ФІЛЬТРАЦІЯ
        # --------------------------

        text_raw = msg.message or ""

        reason = is_blocked_content(text_raw)
        if reason:
            logging.info(f"🚫 Blocked {chat_id}:{msg.id} — {reason}")
            mark_processed(chat_id, msg.id)
            return

        # --------------------------
        # ЧИСТКА ТЕКСТУ ДЛЯ ВІДПРАВКИ
        # --------------------------
        # спершу видалити entities
        text_clean, _ = strip_entities(msg)

# прибрати емодзі
        text_clean = remove_emojis(text_clean)

# ФІЛЬТР САМЕ ТУТ!!
        reason = is_blocked_content(text_clean)
        if reason:
            logging.info(f"🚫 Blocked {chat_id}:{msg.id} — {reason}")
            mark_processed(chat_id, msg.id)
            return

        # обрізання довгого тексту
        if text_clean and len(text_clean) > 1024:
            text_clean = text_clean[:1021] + "..."

        if msg.media:
            if isinstance(msg.media, MessageMediaWebPage):
                if text_clean:
                    await client.send_message(TARGET_CHANNEL, text_clean)

            elif isinstance(msg.media, (MessageMediaPhoto, MessageMediaDocument)):
                caption = text_clean if text_clean else None
                await client.send_file(TARGET_CHANNEL, msg.media, caption=caption)

            else:
                if text_clean:
                    await client.send_message(TARGET_CHANNEL, text_clean)

        else:
            if text_clean:
                await client.send_message(TARGET_CHANNEL, text_clean)

        mark_processed(chat_id, msg.id)
        logging.info(f"✓ Forwarded {chat_id}:{msg.id}")

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
                    async for msg in client.iter_messages(entity, limit=10):
                        if not is_processed(msg.chat_id, msg.id):
                            await forward_message(msg, msg.chat_id)
                except Exception as e:
                    logging.warning(f"⚠️ Poller failed for {src}: {e}")

            logging.info(f"⏱ Poll cycle complete. Sleeping {POLL_INTERVAL} seconds...")
            await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logging.error(f"🔥 Poller loop error: {e}")
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
        logging.info("✅ Connected to Telegram")

        # -------------------------
        # LOAD SOURCE CHANNEL ENTITIES WITH LOGGING
        # -------------------------
        logging.info("🔌 Connecting to source channels...")

        for src in SOURCE_CHANNELS:
            try:
                entity = await client.get_entity(src)
                title = getattr(entity, "title", None)
                if title:
                    logging.info(f"   ✅ Loaded entity for {src} ({title})")
                else:
                    logging.info(f"   ✅ Loaded entity for {src}")
            except Exception as e:
                logging.warning(f"   ⚠️ Could not load entity for {src}: {e}")

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
