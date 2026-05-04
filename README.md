# Purchase Tracker Telegram Agent

Telegram-бот, который принимает сообщения о расходах, отправляет их в LLM, а LLM вызывает tools локального MCP-сервера `purchase-tracker-mcp`. MCP-сервер уже работает с SQLite-БД покупок.

Бот поддерживает несколько Telegram-пользователей поверх одной общей SQLite-БД: в каждый MCP-вызов автоматически прокидывается Telegram `user_id`, поэтому сервер может хранить покупки всех пользователей вместе, но возвращать каждому только его данные.

Схема:

```text
Telegram → этот бот → Alice AI LLM / OpenAI-compatible API → MCP client → purchase-tracker-mcp → SQLite
```

## Что умеет

- Записывать покупки из обычного текста: `кофе 250`, `вчера такси 740`, `пятёрочка 1300 продукты`.
- Записывать несколько покупок из одного сообщения.
- Исправлять и удалять покупки: `удали последнюю`, `исправь последнюю на 270`.
- Смотреть расходы: `сколько ушло на кафе за месяц`, `траты сегодня по категориям`.
- Управлять категориями и лимитами через MCP tools.
- Работать через native tool calling, если провайдер LLM его поддерживает.
- Автоматически откатываться в JSON tool protocol, если native tool calling не поддерживается.
- Передавать Telegram `user_id` во все пользовательские MCP-запросы, чтобы разделять данные разных людей в одной БД.

## Установка рядом с MCP-сервером

Предположим, рядом лежат две папки:

```text
workdir/
├── purchase_tracker_mcp_project/
└── purchase_tracker_telegram_agent/
```

Сначала поставь MCP-сервер из предыдущего проекта:

```bash
cd workdir/purchase_tracker_mcp_project
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Проверь, что команда MCP-сервера доступна:

```bash
purchase-tracker-mcp
```

Останови её через `Ctrl+C`: при обычном запуске она ждёт MCP-клиента по stdio.

Теперь поставь Telegram-агента. Можно использовать тот же virtualenv, чтобы агент видел команду `purchase-tracker-mcp`:

```bash
cd ../purchase_tracker_telegram_agent
pip install -e .
cp .env.example .env
```

## Настройка `.env`

Открой `.env` и заполни значения.

Минимальный набор для Yandex AI Studio / Alice AI LLM:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=

LLM_BASE_URL=https://llm.api.cloud.yandex.net/v1
LLM_API_KEY=
YANDEX_FOLDER_ID=
LLM_MODEL=
LLM_AUTH_HEADER_MODE=api-key
LLM_TOOL_MODE=auto

MCP_SERVER_COMMAND=purchase-tracker-mcp
MCP_SERVER_ARGS=
PURCHASE_DB_PATH=

AGENT_TIMEZONE=Europe/Vilnius
AGENT_DEFAULT_CURRENCY=RUB
```

Если `LLM_MODEL` оставить пустым, бот сам соберёт модель из `YANDEX_FOLDER_ID` и имени модели `aliceai-llm`.

Для другого OpenAI-совместимого шлюза поменяй `LLM_BASE_URL`, `LLM_MODEL` и `LLM_AUTH_HEADER_MODE` в `.env`. Например, для шлюза с Bearer-авторизацией ставь `LLM_AUTH_HEADER_MODE=bearer`.

## Telegram-токен

1. Открой Telegram.
2. Напиши `@BotFather`.
3. Выполни `/newbot`.
4. Скопируй токен в `TELEGRAM_BOT_TOKEN`.
5. Запусти бота и напиши ему `/whoami`.
6. Скопируй свой id в `TELEGRAM_ALLOWED_USER_IDS`, чтобы бот не был доступен посторонним.

Формат для нескольких пользователей:

```env
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
```

Если в белый список добавлено несколько Telegram id, все эти люди смогут пользоваться одним ботом. При этом бот будет автоматически передавать их Telegram `user_id` в MCP-сервер, чтобы каждый пользователь видел только свои покупки и свои агрегаты, даже если физически данные лежат в одной SQLite-БД.

## Проверка MCP

```bash
purchase-agent-smoke
```

Ожидаемый результат: список tools и `health` с путём к SQLite-БД.

Важно: smoke-check проверяет доступность MCP и базовую связность, но не эмулирует Telegram `user_id`. Для проверки multi-user сценария лучше после запуска бота написать ему с двух разных разрешённых аккаунтов и убедиться, что `/recent` и `/today` показывают разные данные.

## Запуск

```bash
purchase-agent-bot
```

Или так:

```bash
python3 -m purchase_agent.main
```

## Команды в Telegram

```text
/start — краткое описание.
/help — помощь.
/whoami — показать Telegram user id.
/health — проверить MCP и БД.
/recent 10 — последние покупки.
/today — расходы за сегодня по категориям.
/month — отчёт по лимитам за текущий месяц.
/categories — список категорий.
/backup — сделать резервную копию SQLite-БД.
/reset_context — сбросить контекст диалога, не трогая БД.
```

## Примеры сообщений

```text
кофе 250
пятёрочка 1300 продукты
вчера такси 740
запиши: озон 2490, одежда
мак 860 кафе, карта тинькофф
удали последнюю
исправь последнюю категорию на Кафе и рестораны
сколько я потратила сегодня
покажи расходы по категориям за месяц
```

## Режимы tool calling

Настройка `LLM_TOOL_MODE`:

```text
auto   — сначала native tools, если ошибка — fallback в JSON protocol.
native — только OpenAI-style function/tool calling.
json   — tools описываются в промпте, модель возвращает JSON-команду.
```

Для максимальной совместимости оставь:

```env
LLM_TOOL_MODE=auto
```

Если провайдер Alice AI не принимает параметр `tools`, поставь:

```env
LLM_TOOL_MODE=json
```

## Как бот вызывает MCP

Бот поднимает MCP-сервер сам через stdio-команду:

```env
MCP_SERVER_COMMAND=purchase-tracker-mcp
MCP_SERVER_ARGS=
```

Если MCP-сервер нужно запускать через Python-модуль:

```env
MCP_SERVER_COMMAND=python3
MCP_SERVER_ARGS=-m purchase_tracker_mcp.server
```

Если хочешь явно задать общий файл БД:

```env
PURCHASE_DB_PATH=/Users/username/.purchase_tracker_mcp/purchases.sqlite3
```

Как работает multi-user flow:

```text
Telegram message → from_user.id → purchase_tracker_telegram_agent → MCP tool arguments + user_id → purchase-tracker-mcp → одна SQLite-БД
```

Предполагается, что MCP-сервер умеет принимать поле `user_id` и использовать его как фильтр почти для всех пользовательских операций: запись покупки, списки, сводки, отчёты по категориям и т.д. Сам агент не хранит отдельные файлы БД по пользователям — разделение делается на стороне MCP/БД.

Если какой-то технический MCP tool не должен принимать `user_id`, это нужно учитывать в схеме и реализации MCP-сервера.

## Установка как systemd service

Скопируй пример:

```bash
mkdir -p /Users/username/.config/systemd/user
cp systemd/purchase-agent-bot.service /Users/username/.config/systemd/user/purchase-agent-bot.service
systemctl --user daemon-reload
systemctl --user enable purchase-agent-bot
systemctl --user start purchase-agent-bot
systemctl --user status purchase-agent-bot
```

В файле `systemd/purchase-agent-bot.service` путь рассчитан на папку:

```text
/Users/username/purchase_tracker_telegram_agent
```

Если проект лежит в другом месте, поменяй `WorkingDirectory`, `EnvironmentFile`, `ExecStart`.

## Безопасность

- Не коммить `.env`.
- Обязательно заполни `TELEGRAM_ALLOWED_USER_IDS` после первого `/whoami`.
- Не отправляй токен Telegram и API-ключ LLM в чат.
- Если разрешаешь нескольким людям пользоваться ботом, проверь, что MCP-сервер действительно фильтрует данные по `user_id`, а не просто принимает это поле формально.
- `purge_all_data` защищён в MCP-сервере точной фразой подтверждения, но лучше не просить агента полностью очищать БД без необходимости.

## Что менять под себя

- Системный промпт: `purchase_agent/prompts.py`.
- Список команд Telegram: `purchase_agent/bot.py`.
- Логика tool loop: `purchase_agent/llm_client.py`.
- MCP transport и сериализация результатов: `purchase_agent/mcp_client.py`.
