from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

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
        connection.commit()


@router.business_connection()
async def on_business_connection(connection: BusinessConnection) -> None:
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
    sender = message.from_user.full_name if message.from_user else "Неизвестный отправитель"
    sender_id = message.from_user.id if message.from_user else message.chat.id
    text = message.text or message.caption or f"[{message.content_type}]"
    with closing(db()) as database:
        database.execute(
            """INSERT OR REPLACE INTO messages
               (business_connection_id, chat_id, message_id, sender_name, sender_id,
                text, message_date, is_deleted)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (message.business_connection_id, message.chat.id, message.message_id,
             sender, sender_id, text, message.date.isoformat()),
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


@router.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted) -> None:
    deleted_ids = list(event.message_ids)
    if not deleted_ids:
        return
    placeholders = ",".join("?" for _ in deleted_ids)
    with closing(db()) as database:
        rows = database.execute(
            f"""SELECT message_id, sender_name, sender_id, text, message_date FROM messages
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
    if rows:
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
            await bot.send_message(
                owner[0],
                f"🗑 Сообщение удалено\nОт: {row['sender_name']}\n"
                f"Время: {row['message_date']}\n\n{row['text']}",
                reply_markup=reply_markup,
            )
    else:
        await bot.send_message(owner[0], "🗑 Сообщение удалено, но копия не была сохранена.")


@app.get("/")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(default=None)) -> dict[str, bool]:
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    payload = await request.json()
    await dispatcher.feed_update(bot, Update.model_validate(payload))
    return {"ok": True}


@app.on_event("startup")
async def startup() -> None:
    init_db()
    if WEBHOOK_URL:
        await bot.set_webhook(
            f"{WEBHOOK_URL}/telegram/webhook",
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["business_connection", "business_message", "edited_business_message", "deleted_business_messages"],
        )


@app.on_event("shutdown")
async def shutdown() -> None:
    await bot.delete_webhook()
    await bot.session.close()
