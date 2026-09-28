from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram-business-archive")

load_dotenv()

# Python 3.9 вместе с uvloop может не создать event loop автоматически.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
DB_PATH = Path(os.environ.get("DATABASE_PATH", "data/messages.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

bot = Bot(BOT_TOKEN)
dispatcher = Dispatcher()
router = Router()
dispatcher.include_router(router)
app = FastAPI(title="Telegram Business message archive")
MASS_DELETE_THRESHOLD = int(os.environ.get("MASS_DELETE_THRESHOLD", "10"))


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with closing(db()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS connections (
                business_connection_id TEXT PRIMARY KEY,
                owner_chat_id INTEGER NOT NULL,
                is_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS messages (
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sender_name TEXT,
                sender_id INTEGER,
                text TEXT,
                media_type TEXT,
                media_file_id TEXT,
                message_date TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (business_connection_id, chat_id, message_id)
            );
            """
        )
        # Безопасная миграция уже созданной SQLite-базы.
        columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        if "sender_id" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN sender_id INTEGER")
        if "media_type" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN media_type TEXT")
        if "media_file_id" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN media_file_id TEXT")
        connection.commit()


def extract_media(message: Message) -> tuple[Optional[str], Optional[str]]:
    """Return the Telegram file type and reusable file_id for a message."""
    if message.voice:
        return "voice", message.voice.file_id
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.document:
        return "document", message.document.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    return None, None


async def ensure_connection(connection_id: str) -> None:
    """Восстанавливает владельца после перезапуска Render."""
    connection = await bot.get_business_connection(connection_id)
    with closing(db()) as database:
        database.execute(
            """INSERT INTO connections(business_connection_id, owner_chat_id, is_enabled)
               VALUES (?, ?, ?)
               ON CONFLICT(business_connection_id) DO UPDATE SET
                 owner_chat_id=excluded.owner_chat_id,
                 is_enabled=excluded.is_enabled""",
            (connection.id, connection.user_chat_id, int(connection.is_enabled)),
        )
        database.commit()


@router.business_connection()
async def on_business_connection(connection: BusinessConnection) -> None:
    logger.info("Business connection update: id=%s enabled=%s", connection.id, connection.is_enabled)
    with closing(db()) as database:
        database.execute(
            """INSERT INTO connections(business_connection_id, owner_chat_id, is_enabled)
               VALUES (?, ?, ?)
               ON CONFLICT(business_connection_id) DO UPDATE SET
                 owner_chat_id=excluded.owner_chat_id,
                 is_enabled=excluded.is_enabled""",
            (connection.id, connection.user_chat_id, int(connection.is_enabled)),
        )
        database.commit()


@router.business_message()
async def on_business_message(message: Message) -> None:
    if not message.business_connection_id or not message.chat:
        return
    logger.info("Business message update: connection=%s chat=%s message=%s",
                message.business_connection_id, message.chat.id, message.message_id)
    await ensure_connection(message.business_connection_id)
    sender = message.from_user.full_name if message.from_user else "Неизвестный отправитель"
    sender_id = message.from_user.id if message.from_user else message.chat.id
    text = message.text or message.caption or f"[{message.content_type}]"
    media_type, media_file_id = extract_media(message)
    with closing(db()) as database:
        database.execute(
            """INSERT OR REPLACE INTO messages
               (business_connection_id, chat_id, message_id, sender_name, sender_id,
                text, media_type, media_file_id, message_date, is_deleted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (message.business_connection_id, message.chat.id, message.message_id,
             sender, sender_id, text, media_type, media_file_id, message.date.isoformat()),
        )
        database.commit()


@router.edited_business_message()
async def on_edited_business_message(message: Message) -> None:
    if not message.business_connection_id or not message.chat:
        return
    text = message.text or message.caption or f"[{message.content_type}]"
    with closing(db()) as database:
        database.execute(
            "UPDATE messages SET text=? WHERE business_connection_id=? AND chat_id=? AND message_id=?",
            (text, message.business_connection_id, message.chat.id, message.message_id),
        )
        owner = database.execute(
            "SELECT owner_chat_id FROM connections WHERE business_connection_id=?",
            (message.business_connection_id,),
        ).fetchone()
        database.commit()
    if owner:
        await bot.send_message(owner[0], f"✏️ Сообщение изменено в чате {message.chat.id}:\n{text}")


def build_chat_backup(rows: list[sqlite3.Row], connection_id: str, chat_id: int) -> bytes:
    """Serialize deleted messages into one portable backup file."""
    backup = {
        "format": "telegram-business-archive",
        "version": 1,
        "business_connection_id": connection_id,
        "chat_id": chat_id,
        "message_count": len(rows),
        "messages": [
            {
                "message_id": row["message_id"],
                "sender_name": row["sender_name"],
                "sender_id": row["sender_id"],
                "text": row["text"],
                "media_type": row["media_type"],
                "media_file_id": row["media_file_id"],
                "message_date": row["message_date"],
            }
            for row in sorted(rows, key=lambda item: item["message_id"])
        ],
    }
    return json.dumps(backup, ensure_ascii=False, indent=2).encode("utf-8")


async def send_mass_delete_backup(owner_chat_id: int, rows: list[sqlite3.Row],
                                  connection_id: str, chat_id: int) -> None:
    backup = build_chat_backup(rows, connection_id, chat_id)
    filename = f"chat_{chat_id}_backup.json"
    document = BufferedInputFile(backup, filename=filename)
    await bot.send_document(
        owner_chat_id,
        document=document,
        caption=(
            f"🗂 Резервная копия удалённого чата\n"
            f"Сообщений: {len(rows)}\n"
            "Текст и данные медиа сохранены в одном JSON-файле."
        ),
    )


@router.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted) -> None:
    deleted_ids = list(event.message_ids)
    if not deleted_ids:
        return
    logger.info("Deleted business messages update: connection=%s chat=%s ids=%s",
                event.business_connection_id, event.chat.id, deleted_ids)
    await ensure_connection(event.business_connection_id)
    placeholders = ",".join("?" for _ in deleted_ids)
    with closing(db()) as database:
        rows = database.execute(
            f"""SELECT message_id, sender_name, sender_id, text, media_type, media_file_id,
                       message_date FROM messages
                WHERE business_connection_id=? AND chat_id=? AND message_id IN ({placeholders})""",
            [event.business_connection_id, event.chat.id, *deleted_ids],
        ).fetchall()
        database.execute(
            f"""UPDATE messages SET is_deleted=1
                WHERE business_connection_id=? AND chat_id=? AND message_id IN ({placeholders})""",
            [event.business_connection_id, event.chat.id, *deleted_ids],
        )
        owner = database.execute(
            "SELECT owner_chat_id FROM connections WHERE business_connection_id=?",
            (event.business_connection_id,),
        ).fetchone()
        database.commit()
    if not owner:
        return
    if rows and len(deleted_ids) >= MASS_DELETE_THRESHOLD:
        await send_mass_delete_backup(
            owner[0], rows, event.business_connection_id, event.chat.id
        )
    elif rows:
        for row in rows:
            reply_markup = None
            if row["sender_id"]:
                reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="Открыть чат",
                            url=f"tg://user?id={row['sender_id']}",
                        )
                    ]]
                )
            header = (
                f"🗑 Сообщение удалено\nОт: {row['sender_name']}\n"
                f"Время: {row['message_date']}"
            )
            body = row["text"] or ""
            caption = f"{header}\n\n{body}".strip()
            media_type = row["media_type"]
            media_file_id = row["media_file_id"]
            if not media_type or not media_file_id:
                await bot.send_message(owner[0], caption, reply_markup=reply_markup)
                continue

            # Telegram captions are limited to 1024 characters for media.
            caption = caption[:1024]
            if media_type == "voice":
                await bot.send_voice(owner[0], media_file_id, caption=caption, reply_markup=reply_markup)
            elif media_type == "photo":
                await bot.send_photo(owner[0], media_file_id, caption=caption, reply_markup=reply_markup)
            elif media_type == "video":
                await bot.send_video(owner[0], media_file_id, caption=caption, reply_markup=reply_markup)
            elif media_type == "audio":
                await bot.send_audio(owner[0], media_file_id, caption=caption, reply_markup=reply_markup)
            elif media_type == "document":
                await bot.send_document(owner[0], media_file_id, caption=caption, reply_markup=reply_markup)
            elif media_type == "animation":
                await bot.send_animation(owner[0], media_file_id, caption=caption, reply_markup=reply_markup)
            elif media_type == "video_note":
                await bot.send_message(owner[0], caption, reply_markup=reply_markup)
                await bot.send_video_note(owner[0], media_file_id)
            else:
                await bot.send_message(owner[0], caption, reply_markup=reply_markup)
    else:
        await bot.send_message(owner[0], "🗑 Сообщение удалено, но копия не была сохранена.")


@app.get("/")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    payload = await request.json()
    logger.info("Telegram update received: update_id=%s keys=%s",
                payload.get("update_id"), [key for key in payload if key != "update_id"])
    await dispatcher.feed_update(bot, Update.model_validate(payload))
    return {"ok": True}


@app.on_event("startup")
async def startup() -> None:
    init_db()
    if WEBHOOK_URL:
        await bot.set_webhook(
            f"{WEBHOOK_URL}/telegram/webhook",
            secret_token=None,
            allowed_updates=["business_connection", "business_message", "edited_business_message", "deleted_business_messages"],
        )


@app.on_event("shutdown")
async def shutdown() -> None:
    # Не удаляем webhook при перезапуске Render: старый процесс может
    # остановиться уже после запуска нового и отключить рабочий webhook.
    await bot.session.close()
