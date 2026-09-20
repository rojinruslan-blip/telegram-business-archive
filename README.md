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

Токен не добавляй в GitHub и не отправляй в чат.
