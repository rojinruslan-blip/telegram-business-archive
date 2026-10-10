from __future__ import annotations


import asyncio
import io
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional


from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
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
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
SETUP_VIDEO_FILE_ID = os.environ.get("SETUP_VIDEO_FILE_ID", "")
DEMO_VIDEO_FILE_ID = os.environ.get("DEMO_VIDEO_FILE_ID", "")
WELCOME_PHOTO_FILE_ID = os.environ.get("WELCOME_PHOTO_FILE_ID", "")
SETUP_PHOTO_FILE_ID = os.environ.get("SETUP_PHOTO_FILE_ID", "")
CHAT_AUTOMATION_USERNAME = os.environ.get("CHAT_AUTOMATION_USERNAME", "@Undelink_bot")
ASSET_BASE_URL = os.environ.get(
    "ASSET_BASE_URL",
    "https://raw.githubusercontent.com/rojinruslan-blip/telegram-business-archive/main",
).rstrip("/")
DEFAULT_ASSETS = {
    "welcome": f"{ASSET_BASE_URL}/codex-clipboard-78758548-3c2f-4d60-9628-58ec986ea201.png",
    "instruction": f"{ASSET_BASE_URL}/codex-clipboard-bdbc7038-cd27-4da9-b761-c750e1841129.png",
}
# Render Free clears the local SQLite file on each deploy.  Keep the default
# welcome copy in the repository so a deploy never reverts users to the old
# technical text before they have uploaded another custom banner.
DEFAULT_ASSET_CAPTIONS = {
    "welcome": (
        "🤖 Этот бот поможет тебе видеть:\n\n"
        "удалённые сообщения 🗑\n"
        "голосовые сообщения 📣\n"
        "видеосообщения 👀\n"
        "фотографии с таймером 💀\n\n"
        "Быстрый старт:\n"
        "• Нажми кнопку «Подключить» ниже. Откроется окно редактирования профиля.\n"
        "• Найди «Автоматизация чатов».\n"
        "• Введи @Undelink_bot для подключения.\n\n"
        "Если возникнут проблемы с подключением — нажми «Инструкция»."
    ),
    "instruction": "",
}


bot = Bot(BOT_TOKEN)
dispatcher = Dispatcher()
router = Router()
dispatcher.include_router(router)
app = FastAPI(title="Telegram Business message archive")
MASS_DELETE_THRESHOLD = int(os.environ.get("MASS_DELETE_THRESHOLD", "10"))




class PostgresConnection:
    """Keeps the SQLite-style queries usable with Render Postgres."""


    def __init__(self, url: str) -> None:
        import psycopg
        from psycopg.rows import dict_row


        self.connection = psycopg.connect(url, row_factory=dict_row)


    def execute(self, query: str, params: tuple | list = ()):
        return self.connection.execute(query.replace("?", "%s"), params)


    def commit(self) -> None:
        self.connection.commit()


    def close(self) -> None:
        self.connection.close()




def db() -> sqlite3.Connection | PostgresConnection:
    if DATABASE_URL:
        return PostgresConnection(DATABASE_URL)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection




def init_db() -> None:
    with closing(db()) as connection:
        schema = """
            CREATE TABLE IF NOT EXISTS connections (
                business_connection_id TEXT PRIMARY KEY,
                owner_chat_id BIGINT NOT NULL,
                is_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS messages (
                business_connection_id TEXT NOT NULL,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                sender_name TEXT,
                sender_id BIGINT,
                text TEXT,
                media_type TEXT,
                media_file_id TEXT,
                media_bytes BYTEA,
                message_date TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (business_connection_id, chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS owner_settings (
                owner_chat_id BIGINT PRIMARY KEY,
                notify_edits INTEGER NOT NULL DEFAULT 1,
                notify_deletions INTEGER NOT NULL DEFAULT 1,
                media_timers INTEGER NOT NULL DEFAULT 1,
                incognito INTEGER NOT NULL DEFAULT 0,
                old_media INTEGER NOT NULL DEFAULT 0,
                archive_enabled INTEGER NOT NULL DEFAULT 1,
                retention_days INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bot_assets (
                name TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                caption_entities TEXT NOT NULL DEFAULT '[]'
            );
        """
        if DATABASE_URL:
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
        else:
            connection.executescript(schema)
        if DATABASE_URL:
            # Telegram chat/user IDs exceed PostgreSQL INTEGER (32-bit).
            # Upgrade databases created by the first Postgres migration too.
            for table, column in (
                ("connections", "owner_chat_id"),
                ("messages", "chat_id"),
                ("messages", "message_id"),
                ("messages", "sender_id"),
                ("owner_settings", "owner_chat_id"),
            ):
                connection.execute(
                    f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT"
                )
            connection.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_bytes BYTEA")
            connection.commit()
            return
        # Безопасная миграция уже созданной SQLite-базы.
        columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        if "sender_id" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN sender_id INTEGER")
        if "media_type" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN media_type TEXT")
        if "media_file_id" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN media_file_id TEXT")
        if "media_bytes" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN media_bytes BLOB")
        asset_columns = {row[1] for row in connection.execute("PRAGMA table_info(bot_assets)")}
        if "caption" not in asset_columns:
            connection.execute("ALTER TABLE bot_assets ADD COLUMN caption TEXT NOT NULL DEFAULT ''")
        if "caption_entities" not in asset_columns:
            connection.execute(
                "ALTER TABLE bot_assets ADD COLUMN caption_entities TEXT NOT NULL DEFAULT '[]'"
            )
        connection.commit()




def ensure_owner_settings(owner_chat_id: int) -> None:
    with closing(db()) as connection:
        connection.execute(
            "INSERT INTO owner_settings(owner_chat_id) VALUES (?) ON CONFLICT(owner_chat_id) DO NOTHING",
            (owner_chat_id,),
        )
        connection.commit()




def owner_settings(owner_chat_id: int) -> sqlite3.Row:
    ensure_owner_settings(owner_chat_id)
    with closing(db()) as connection:
        return connection.execute(
            "SELECT * FROM owner_settings WHERE owner_chat_id=?",
            (owner_chat_id,),
        ).fetchone()




def set_owner_setting(owner_chat_id: int, field: str, value: int) -> None:
    allowed = {
        "notify_edits", "notify_deletions", "media_timers", "incognito",
        "old_media", "archive_enabled", "retention_days",
    }
    if field not in allowed:
        raise ValueError(f"Unsupported owner setting: {field}")
    ensure_owner_settings(owner_chat_id)
    with closing(db()) as connection:
        connection.execute(
            f"UPDATE owner_settings SET {field}=? WHERE owner_chat_id=?",
            (value, owner_chat_id),
        )
        connection.commit()




def get_asset(name: str) -> str:
    configured = {
        "welcome": WELCOME_PHOTO_FILE_ID,
        "instruction": SETUP_PHOTO_FILE_ID,
    }.get(name, "")
    if configured:
        return configured
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT file_id FROM bot_assets WHERE name=?", (name,)
        ).fetchone()
    # Render's free filesystem is temporary and is cleared on a deploy.  The
    # versioned image is the durable fallback; a photo sent to the bot or an
    # environment variable can still override it.
    return row["file_id"] if row else DEFAULT_ASSETS.get(name, "")




def get_asset_caption(name: str) -> str:
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT caption FROM bot_assets WHERE name=?", (name,)
        ).fetchone()
    return row["caption"] if row else DEFAULT_ASSET_CAPTIONS.get(name, "")




def get_asset_caption_entities(name: str) -> list[MessageEntity]:
    with closing(db()) as connection:
        row = connection.execute(
            "SELECT caption_entities FROM bot_assets WHERE name=?", (name,)
        ).fetchone()
    if not row or not row["caption_entities"]:
        return []
    return [MessageEntity.model_validate(entity) for entity in json.loads(row["caption_entities"])]




def save_asset(
    name: str,
    file_id: str,
    caption: str = "",
    caption_entities: Optional[list[MessageEntity]] = None,
) -> None:
    entities_json = json.dumps(
        [entity.model_dump(mode="json") for entity in (caption_entities or [])]
    )
    with closing(db()) as connection:
        connection.execute(
            "INSERT INTO bot_assets(name, file_id, caption, caption_entities) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET file_id=excluded.file_id, caption=excluded.caption, "
            "caption_entities=excluded.caption_entities",
            (name, file_id, caption, entities_json),
        )
        connection.commit()




def archive_stats(owner_chat_id: int) -> tuple[int, int]:
    with closing(db()) as connection:
        row = connection.execute(
            """SELECT COUNT(*) AS total, COALESCE(SUM(is_deleted), 0) AS deleted
               FROM messages m JOIN connections c
                 ON c.business_connection_id=m.business_connection_id
               WHERE c.owner_chat_id=?""",
            (owner_chat_id,),
        ).fetchone()
    return int(row["total"]), int(row["deleted"])




def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🛡 Подключить", url="tg://settings/edit", style="success"
            ),
            InlineKeyboardButton(
                text="⚙️ Инструкция", callback_data="menu:tutorial", style="danger"
            ),
        ],
        [InlineKeyboardButton(
            text="🔔 Уведомления", callback_data="menu:settings", style="primary"
        )],
    ])




def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="menu:home", style="primary")
    ]])




def settings_keyboard(settings: sqlite3.Row) -> InlineKeyboardMarkup:
    mark = lambda enabled: "✅" if settings[enabled] else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✏️ Изменения {mark('notify_edits')}", callback_data="toggle:notify_edits")],
        [InlineKeyboardButton(text=f"🗑 Удаления {mark('notify_deletions')}", callback_data="toggle:notify_deletions")],
        [InlineKeyboardButton(text=f"📸 Медиа с таймером {mark('media_timers')}", callback_data="toggle:media_timers")],
        [InlineKeyboardButton(text=f"🥷 Инкогнито {mark('incognito')}", callback_data="toggle:incognito")],
        [InlineKeyboardButton(text=f"📅 Старое медиа {mark('old_media')}", callback_data="toggle:old_media")],
        [InlineKeyboardButton(text=f"🗃 Архив {mark('archive_enabled')}", callback_data="toggle:archive_enabled")],
        [InlineKeyboardButton(text="🗄 Срок хранения", callback_data="menu:retention")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:home")],
    ])




def retention_keyboard(settings: sqlite3.Row) -> InlineKeyboardMarkup:
    current = "без ограничений" if not settings["retention_days"] else f"{settings['retention_days']} дн."
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней", callback_data="retention:7"),
         InlineKeyboardButton(text="30 дней", callback_data="retention:30")],
        [InlineKeyboardButton(text="90 дней", callback_data="retention:90"),
         InlineKeyboardButton(text="Без ограничений", callback_data="retention:0")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:settings")],
    ])




async def send_home(chat_id: int) -> None:
    text = (
        "🛡 Этот бот сохраняет удалённые и изменённые сообщения в чатах.\n\n"
        "Для подключения откройте настройки Telegram → Изменить → "
        "Автоматизация чатов и добавьте бота.\n"
        f"Введите: {CHAT_AUTOMATION_USERNAME}\n\n"
        "После подключения бот будет сохранять входящие и исходящие сообщения "
        "при наличии доступа к ним."
    )
    welcome_photo = get_asset("welcome")
    welcome_caption = get_asset_caption("welcome") or text
    welcome_caption_entities = get_asset_caption_entities("welcome")
    if welcome_photo:
        await bot.send_photo(
            chat_id,
            welcome_photo,
            caption=welcome_caption,
            caption_entities=welcome_caption_entities or None,
            reply_markup=menu_keyboard(),
        )
    else:
        await bot.send_message(chat_id, text, reply_markup=menu_keyboard())




@router.message(CommandStart())
async def on_start(message: Message) -> None:
    ensure_owner_settings(message.from_user.id)
    await send_home(message.chat.id)




@router.message(F.photo)
async def on_asset_photo(message: Message) -> None:
    """Save photos sent to the bot with Welcome/Инструкция captions."""
    if message.chat.type != "private" or not message.photo:
        return
    raw_caption = (message.caption or "").strip()
    normalized_caption = raw_caption.casefold()
    markers = {
        "welcome": ("welcome", "приветственное фото"),
        "приветствие": ("welcome", "приветственное фото"),
        "инструкция": ("instruction", "фото-инструкция"),
        "instruction": ("instruction", "фото-инструкция"),
        "setup": ("instruction", "фото-инструкция"),
    }
    matched_marker = next(
        (marker for marker in markers
         if normalized_caption == marker
         or normalized_caption.startswith((marker + " ", marker + ":", marker + "\n", marker + "—"))),
        None,
    )
    if not matched_marker:
        return
    asset_name, label = markers[matched_marker]
    custom_caption = raw_caption[len(matched_marker):].lstrip(" :\n—-")
    removed_prefix = raw_caption[:len(raw_caption) - len(custom_caption)]
    removed_utf16_length = len(removed_prefix.encode("utf-16-le")) // 2
    caption_entities = [
        entity.model_copy(update={"offset": entity.offset - removed_utf16_length})
        for entity in (message.caption_entities or [])
        if entity.offset >= removed_utf16_length
    ]
    save_asset(
        asset_name,
        message.photo[-1].file_id,
        custom_caption,
        caption_entities,
    )
    await message.answer(f"✅ Сохранено: {label}")




@router.callback_query(F.data == "menu:home")
async def menu_home(callback: CallbackQuery) -> None:
    if callback.message.photo:
        await callback.message.delete()
    else:
        await callback.message.edit_text(
            "🛡 Главное меню бота:", reply_markup=menu_keyboard()
        )
    await callback.answer()


@router.callback_query(F.data.in_({"menu:setup", "menu:tutorial"}))
async def menu_setup(callback: CallbackQuery) -> None:
    text = (
        "❓ Как подключить бота\n\n"
        "1. Откройте настройки Telegram.\n"
        "2. Нажмите «Изменить».\n"
        "3. Откройте «Автоматизация чатов».\n"
        f"4. Введите {CHAT_AUTOMATION_USERNAME} и выберите этого бота.\n"
        "5. Разрешите доступ к сообщениям, изменениям и удалениям."
    )
    setup_photo = get_asset("instruction")
    setup_caption = get_asset_caption("instruction")
    setup_caption_entities = get_asset_caption_entities("instruction")
    if setup_photo:
        await bot.send_photo(
            callback.from_user.id,
            setup_photo,
            caption=setup_caption or None,
            caption_entities=setup_caption_entities or None,
            reply_markup=back_keyboard(),
        )
    elif SETUP_VIDEO_FILE_ID:
        await bot.send_video(callback.from_user.id, SETUP_VIDEO_FILE_ID, caption=text)
    else:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:home")]
        ]))
    await callback.answer()




@router.callback_query(F.data == "menu:support")
async def menu_support(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "👥 Поддержка\n\nСвяжитесь с владельцем бота, если нужна помощь с подключением.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:home")]
        ]),
    )
    await callback.answer()




@router.callback_query(F.data == "menu:demo")
async def menu_demo(callback: CallbackQuery) -> None:
    if DEMO_VIDEO_FILE_ID:
        await bot.send_video(
            callback.from_user.id,
            DEMO_VIDEO_FILE_ID,
            caption="🖼 Демонстрация: бот сохраняет сообщения до их удаления.",
        )
    else:
        await callback.message.edit_text(
            "🖼 Демонстрационный материал подготовим и добавим сюда.\n\n"
            "Сейчас бот уже умеет сохранять удалённые сообщения, изменения и медиа.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:home")]
            ]),
        )
    await callback.answer()




@router.callback_query(F.data == "menu:settings")
async def menu_settings(callback: CallbackQuery) -> None:
    text = (
        "🔔 Настройки уведомлений:\n\n"
        "Выберите, какие события и материалы нужно получать."
    )
    markup = settings_keyboard(owner_settings(callback.from_user.id))
    if callback.message.photo:
        await bot.send_message(callback.from_user.id, text, reply_markup=markup)
    else:
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()




@router.callback_query(F.data.startswith("toggle:"))
async def toggle_setting(callback: CallbackQuery) -> None:
    field = callback.data.split(":", 1)[1]
    allowed = {
        "notify_edits", "notify_deletions", "media_timers", "incognito",
        "old_media", "archive_enabled",
    }
    if field not in allowed:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    settings = owner_settings(callback.from_user.id)
    set_owner_setting(callback.from_user.id, field, 0 if settings[field] else 1)
    await callback.message.edit_reply_markup(
        reply_markup=settings_keyboard(owner_settings(callback.from_user.id))
    )
    await callback.answer("Настройка обновлена")
                    




@router.callback_query(F.data == "menu:archive")
async def menu_archive(callback: CallbackQuery) -> None:
    total, deleted = archive_stats(callback.from_user.id)
    await callback.message.edit_text(
        f"🗃 Архив\n\nВсего сохранено сообщений: {total}\nУдалённых сообщений: {deleted}\n\n"
        "Архив работает автоматически после подключения Telegram Business.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:home")]
        ]),
    )
    await callback.answer()




@router.callback_query(F.data == "menu:retention")
async def menu_retention(callback: CallbackQuery) -> None:
    settings = owner_settings(callback.from_user.id)
    current = "без ограничений" if not settings["retention_days"] else f"{settings['retention_days']} дней"
    await callback.message.edit_text(
        f"🗄 Срок хранения\n\nТекущий срок: {current}",
        reply_markup=retention_keyboard(settings),
    )
    await callback.answer()




@router.callback_query(F.data.startswith("retention:"))
async def set_retention(callback: CallbackQuery) -> None:
    days = int(callback.data.split(":", 1)[1])
    set_owner_setting(callback.from_user.id, "retention_days", days)
    await callback.message.edit_text(
        f"🗄 Срок хранения установлен: {'без ограничений' if not days else f'{days} дней'}",
        reply_markup=retention_keyboard(owner_settings(callback.from_user.id)),
    )
    await callback.answer("Срок хранения обновлён")




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




async def download_media(file_id: Optional[str]) -> Optional[bytes]:
    """Save a copy before Telegram temporary media/file_id expires."""
    if not file_id:
        return None
    try:
        telegram_file = await bot.get_file(file_id)
        buffer = io.BytesIO()
        await bot.download(telegram_file, destination=buffer)
        return buffer.getvalue()
    except Exception:
        logger.exception("Could not download media file_id=%s", file_id)
        return None




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
    ensure_owner_settings(connection.user_chat_id)




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
    ensure_owner_settings(connection.user_chat_id)




@router.business_message()
async def on_business_message(message: Message) -> None:
    if not message.business_connection_id or not message.chat:
        return
    logger.info("Business message update: connection=%s chat=%s message=%s",
                message.business_connection_id, message.chat.id, message.message_id)
    with closing(db()) as database:
        owner = database.execute(
            "SELECT owner_chat_id FROM connections WHERE business_connection_id=?",
            (message.business_connection_id,),
        ).fetchone()
    if not owner:
        await ensure_connection(message.business_connection_id)
        with closing(db()) as database:
            owner = database.execute(
                "SELECT owner_chat_id FROM connections WHERE business_connection_id=?",
                (message.business_connection_id,),
            ).fetchone()
    if owner and not owner_settings(owner["owner_chat_id"])["archive_enabled"]:
        return
    sender = message.from_user.full_name if message.from_user else "Неизвестный отправитель"
    sender_id = message.from_user.id if message.from_user else message.chat.id
    text = message.text or message.caption or f"[{message.content_type}]"
    media_type, media_file_id = extract_media(message)
    media_bytes = await download_media(media_file_id)
    with closing(db()) as database:
        database.execute(
            """INSERT INTO messages
               (business_connection_id, chat_id, message_id, sender_name, sender_id,
                text, media_type, media_file_id, media_bytes, message_date, is_deleted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(business_connection_id, chat_id, message_id) DO UPDATE SET
                 sender_name=excluded.sender_name, sender_id=excluded.sender_id,
                 text=excluded.text, media_type=excluded.media_type,
                 media_file_id=excluded.media_file_id, media_bytes=excluded.media_bytes,
                 message_date=excluded.message_date,
                 is_deleted=0""",
            (message.business_connection_id, message.chat.id, message.message_id,
             sender, sender_id, text, media_type, media_file_id, media_bytes,
             message.date.isoformat()),
        )
        database.commit()




@router.edited_business_message()
async def on_edited_business_message(message: Message) -> None:
    if not message.business_connection_id or not message.chat:
        return
    text = message.text or message.caption or f"[{message.content_type}]"
    sender = message.from_user.full_name if message.from_user else "Неизвестный отправитель"
    sender_id = message.from_user.id if message.from_user else message.chat.id
    with closing(db()) as database:
        database.execute(
            """UPDATE messages SET text=?, sender_name=?, sender_id=?
               WHERE business_connection_id=? AND chat_id=? AND message_id=?""",
            (text, sender, sender_id, message.business_connection_id, message.chat.id, message.message_id),
        )
        owner = database.execute(
            "SELECT owner_chat_id FROM connections WHERE business_connection_id=?",
            (message.business_connection_id,),
        ).fetchone()
        database.commit()
    if owner and owner_settings(owner["owner_chat_id"])["notify_edits"]:
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть чат", url=f"tg://user?id={sender_id}")
        ]])
        await bot.send_message(
            owner["owner_chat_id"],
            f"✏️ Сообщение изменено\nОт: {sender}\n\n{text}",
            reply_markup=reply_markup,
        )




def build_chat_backup(rows: list[sqlite3.Row]) -> bytes:
    """Create a readable transcript of messages removed from a chat."""
    lines = ["Удалённые сообщения", ""]
    for row in sorted(rows, key=lambda item: item["message_id"]):
        media = f" [{row['media_type']}]" if row["media_type"] else ""
        body = row["text"] or "[без текста]"
        lines.extend((f"{row['sender_name'] or 'Неизвестный отправитель'}{media}", body, ""))
    return "\n".join(lines).encode("utf-8")




async def send_mass_delete_backup(owner_chat_id: int, rows: list[sqlite3.Row], chat_id: int) -> None:
    backup = build_chat_backup(rows)
    filename = f"chat_{chat_id}_history.txt"
    document = BufferedInputFile(backup, filename=filename)
    await bot.send_document(
        owner_chat_id,
        document=document,
        caption=(
            f"🗂 История удалённых сообщений\n"
            f"Сообщений: {len(rows)}\n"
            "Текст сообщений сохранён в читаемом файле. Медиа отправлены отдельными сообщениями."
        ),
    )




def deleted_message_markup(row: sqlite3.Row) -> Optional[InlineKeyboardMarkup]:
    if not row["sender_id"]:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть чат", url=f"tg://user?id={row['sender_id']}")
    ]])




async def send_deleted_message(owner_chat_id: int, row: sqlite3.Row) -> None:
    """Send the saved message back, including media that later expired or was deleted."""
    reply_markup = deleted_message_markup(row)
    header = f"🗑 Сообщение удалено\nОт: {row['sender_name'] or 'Неизвестный отправитель'}"
    body = row["text"] or ""
    caption = f"{header}\n\n{body}".strip()
    media_type = row["media_type"]
    media_file_id = row["media_file_id"]
    media_bytes = row["media_bytes"]
    if not media_type or (not media_file_id and not media_bytes):
        await bot.send_message(owner_chat_id, caption, reply_markup=reply_markup)
        return
    media_source = (
        BufferedInputFile(media_bytes, filename=f"restored_{row['message_id']}")
        if media_bytes else media_file_id
    )


    # Telegram captions are limited to 1024 characters for media.
    caption = caption[:1024]
    if media_type == "voice":
        await bot.send_voice(owner_chat_id, media_source, caption=caption, reply_markup=reply_markup)
    elif media_type == "photo":
        await bot.send_photo(owner_chat_id, media_file_id, caption=caption, reply_markup=reply_markup)
    elif media_type == "video":
        await bot.send_video(owner_chat_id, media_file_id, caption=caption, reply_markup=reply_markup)
    elif media_type == "audio":
        await bot.send_audio(owner_chat_id, media_file_id, caption=caption, reply_markup=reply_markup)
    elif media_type == "document":
        await bot.send_document(owner_chat_id, media_file_id, caption=caption, reply_markup=reply_markup)
    elif media_type == "animation":
        await bot.send_animation(owner_chat_id, media_file_id, caption=caption, reply_markup=reply_markup)
    elif media_type == "video_note":
        await bot.send_message(owner_chat_id, caption, reply_markup=reply_markup)
        await bot.send_video_note(owner_chat_id, media_source)
    else:
        await bot.send_message(owner_chat_id, caption, reply_markup=reply_markup)




@router.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted) -> None:
    deleted_ids = list(event.message_ids)
    if not deleted_ids:
        return
    logger.info("Deleted business messages update: connection=%s chat=%s ids=%s",
                event.business_connection_id, event.chat.id, deleted_ids)
    with closing(db()) as database:
        known_connection = database.execute(
            "SELECT business_connection_id FROM connections WHERE business_connection_id=?",
            (event.business_connection_id,),
        ).fetchone()
    if not known_connection:
        await ensure_connection(event.business_connection_id)
    placeholders = ",".join("?" for _ in deleted_ids)
    with closing(db()) as database:
        matched_by_connection_only = False
        rows = database.execute(
            f"""SELECT message_id, sender_name, sender_id, text, media_type, media_file_id,
                       media_bytes, message_date FROM messages
                WHERE business_connection_id=? AND chat_id=? AND message_id IN ({placeholders})
                  AND is_deleted=0""",
            [event.business_connection_id, event.chat.id, *deleted_ids],
        ).fetchall()
        if not rows:
            # В некоторых Business updates Telegram может прислать другой chat_id,
            # хотя connection_id и message_id остаются теми же. Не теряем архив:
            # резервно ищем сообщение в рамках конкретного подключения.
            rows = database.execute(
                f"""SELECT message_id, sender_name, sender_id, text, media_type, media_file_id,
                           media_bytes, message_date FROM messages
                    WHERE business_connection_id=? AND message_id IN ({placeholders})
                      AND is_deleted=0""",
                [event.business_connection_id, *deleted_ids],
            ).fetchall()
            if rows:
                matched_by_connection_only = True
                logger.warning(
                    "Deleted message matched by connection and message_id only: "
                    "connection=%s event_chat=%s ids=%s",
                    event.business_connection_id, event.chat.id, deleted_ids,
                )
        if matched_by_connection_only:
            database.execute(
                f"""UPDATE messages SET is_deleted=1
                    WHERE business_connection_id=? AND message_id IN ({placeholders})""",
                [event.business_connection_id, *deleted_ids],
            )
        else:
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
    # Telegram can retry a deletion update while a deploy is switching over.
    # A message already marked as deleted must never be sent twice.
    if not rows:
        return
    if not owner_settings(owner["owner_chat_id"])["notify_deletions"]:
        return
    if rows and len(deleted_ids) >= MASS_DELETE_THRESHOLD:
        await bot.send_message(
            owner["owner_chat_id"],
            f"🗂 В чате удалено сообщений: {len(rows)}. Сохранённые копии — ниже.",
        )
        await send_mass_delete_backup(owner["owner_chat_id"], rows, event.chat.id)
    for row in rows:
        await send_deleted_message(owner["owner_chat_id"], row)




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
    try:
        await bot.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION)
        await bot.set_my_description(description=BOT_DESCRIPTION)
    except Exception:
        logger.exception("Could not update bot profile description")


    if WEBHOOK_URL:
        await bot.set_webhook(
            f"{WEBHOOK_URL}/telegram/webhook",
            secret_token=None,
            allowed_updates=[
                "message", "callback_query", "business_connection", "business_message",
                "edited_business_message", "deleted_business_messages",
            ],
        )




@app.on_event("shutdown")
async def shutdown() -> None:
    # Не удаляем webhook при перезапуске Render: старый процесс может
    # остановиться уже после запуска нового и отключить рабочий webhook.
    await bot.session.close()
