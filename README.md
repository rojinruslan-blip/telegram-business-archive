# Telegram Business message archive

Минимальный сервер для собственного Telegram Business-бота: сохраняет входящие сообщения из разрешённых чатов и сообщает об изменениях и удалениях.

## Запуск локально

```bash
cd spyundo-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# впиши токен в .env
uvicorn app:app --host 0.0.0.0 --port 8000
```

Для локального теста нужен публичный HTTPS-адрес для webhook (например, через Render или туннель). В Render команда запуска: `uvicorn app:app --host 0.0.0.0 --port $PORT`.

## Переменные окружения

- `BOT_TOKEN` — токен от BotFather.
- `WEBHOOK_SECRET` — случайная строка для защиты webhook.
- `DATABASE_PATH` — путь к SQLite, по умолчанию `data/messages.sqlite3`.
- `WEBHOOK_URL` — публичный HTTPS URL сервиса без завершающего `/`.
- `SETUP_VIDEO_FILE_ID` — необязательный `file_id` видео-инструкции по подключению.
- `DEMO_VIDEO_FILE_ID` — необязательный `file_id` демонстрационного видео.
- `WELCOME_PHOTO_FILE_ID` — необязательный `file_id` приветственного фото.
- `SETUP_PHOTO_FILE_ID` — необязательный `file_id` фото-инструкции.
- `CHAT_AUTOMATION_USERNAME` — username бота для раздела «Автоматизация чатов».

После `/start` бот показывает меню без оплаты и подписок: подключение через «Автоматизацию чатов», инструкция по подключению,
демонстрация, настройки уведомлений, архив и срок хранения. Видео можно один раз
отправить боту, взять его `file_id` из логов или отдельного тестового обработчика и
сохранить в Render как переменную окружения.

Токен не добавляй в GitHub и не отправляй в чат.
