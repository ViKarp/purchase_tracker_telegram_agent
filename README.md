# Purchase Tracker Telegram Agent: деплой на сервере

Telegram-бот для учёта покупок.

Схема работы:

```text
Telegram → purchase_tracker_telegram_agent → LLM → MCP client → purchase_tracker_mcp_project → SQLite
```

MCP-сервер не нужно запускать отдельным постоянным процессом. Агент сам поднимает его через stdio-команду `purchase-tracker-mcp`.

---

## 1. Требования

На сервере нужны:

- Python 3.10+
- git
- доступ в интернет
- Telegram Bot Token
- API-ключ LLM-провайдера
- исходники двух репозиториев из GitHub:
  - `purchase_tracker_mcp_project`
  - `purchase_tracker_telegram_agent`

---

## 2. Установка системных зависимостей

Для Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

Проверь версию Python:

```bash
python3 --version
```

Нужна версия не ниже `3.10`.

---

## 3. Создание рабочей директории

```bash
mkdir -p ~/apps/purchase-tracker
cd ~/apps/purchase-tracker
```

---

## 4. Скачивание исходников из GitHub

```bash
git clone https://github.com/ViKarp/purchase_tracker_mcp_project.git
git clone https://github.com/ViKarp/purchase_tracker_telegram_agent.git
```

После этого структура должна быть такой:

```text
~/apps/purchase-tracker/
├── purchase_tracker_mcp_project/
└── purchase_tracker_telegram_agent/
```

---

## 5. Создание одного общего virtualenv

Важно: MCP-сервер и Telegram-агент лучше ставить в **один и тот же virtualenv**. Тогда агент точно увидит команду `purchase-tracker-mcp`.

```bash
cd ~/apps/purchase-tracker
python3 -m venv .venv
source .venv/bin/activate
```

Обнови базовые инструменты установки:

```bash
pip install --upgrade pip setuptools wheel
```

---

## 6. Установка MCP-сервера

```bash
cd ~/apps/purchase-tracker/purchase_tracker_mcp_project
pip install -e .
```

Проверь, что команда появилась:

```bash
which purchase-tracker-mcp
```

Ожидаемо путь должен быть примерно такой:

```text
/home/vityakarpenko2016/apps/purchase-tracker/.venv/bin/purchase-tracker-mcp
```

Можно проверить ручной запуск:

```bash
purchase-tracker-mcp
```

Если терминал “завис”, это нормально: MCP-сервер ждёт JSON-RPC сообщения от MCP-клиента через stdio.

Останови его:

```bash
Ctrl+C
```

---

## 7. Установка Telegram-агента

```bash
cd ~/apps/purchase-tracker/purchase_tracker_telegram_agent
pip install -e .
```

Проверь, что команды агента появились:

```bash
which purchase-agent-bot
which purchase-agent-smoke
```

Ожидаемые пути:

```text
/home/vityakarpenko2016/apps/purchase-tracker/.venv/bin/purchase-agent-bot
/home/vityakarpenko2016/apps/purchase-tracker/.venv/bin/purchase-agent-smoke
```

---

## 8. Настройка `.env`

Создай файл `.env`:

```bash
cd ~/apps/purchase-tracker/purchase_tracker_telegram_agent
cp .env.example .env
nano .env
```

Минимальный рабочий пример `.env`:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=

LLM_BASE_URL=https://llm.api.cloud.yandex.net/v1
LLM_API_KEY=
LLM_PROJECT=
YANDEX_FOLDER_ID=
LLM_MODEL=
LLM_AUTH_HEADER_MODE=api-key
LLM_TOOL_MODE=json
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=1200
LLM_MAX_TOOL_ITERATIONS=6

MCP_SERVER_COMMAND=/home/vityakarpenko2016/apps/purchase-tracker/.venv/bin/purchase-tracker-mcp
MCP_SERVER_ARGS=

PURCHASE_DB_PATH=/home/vityakarpenko2016/.purchase_tracker_mcp/purchases.sqlite3

AGENT_TIMEZONE=Europe/Moscow
AGENT_DEFAULT_CURRENCY=RUB
AGENT_HISTORY_TURNS=10
LOG_LEVEL=INFO
```

### Что обязательно заполнить

Обязательно заполни:

```env
TELEGRAM_BOT_TOKEN=
LLM_API_KEY=
YANDEX_FOLDER_ID=
```

После первого запуска бота напиши ему `/whoami`, получи свой Telegram id и заполни:

```env
TELEGRAM_ALLOWED_USER_IDS=123456789
```

Если оставить `TELEGRAM_ALLOWED_USER_IDS` пустым, бот будет отвечать любому пользователю, который найдёт бота в Telegram.

---

## 9. Создание директории для SQLite-БД

```bash
mkdir -p ~/.purchase_tracker_mcp
```

Права на `.env` лучше ограничить:

```bash
chmod 600 ~/apps/purchase-tracker/purchase_tracker_telegram_agent/.env
```

---

## 10. Проверка MCP-связности

Активируй virtualenv:

```bash
cd ~/apps/purchase-tracker
source .venv/bin/activate
```

Запусти smoke-check:

```bash
cd ~/apps/purchase-tracker/purchase_tracker_telegram_agent
purchase-agent-smoke
```

Ожидаемый результат:

- список MCP tools;
- успешный ответ `health`;
- путь к SQLite-БД.

Примерно так:

```text
MCP tools:
[
  "health",
  "add_purchase",
  "get_purchase",
  "list_purchases",
  "update_purchase",
  "delete_purchase",
  "list_categories",
  "upsert_category",
  "rename_category",
  "delete_category",
  "get_summary",
  "monthly_budget_report",
  "export_purchases_csv",
  "import_purchases_csv",
  "backup_database",
  "purge_all_data"
]

health:
{
  "ok": true,
  "server": "purchase-tracker",
  "db_path": "/home/vityakarpenko2016/.purchase_tracker_mcp/purchases.sqlite3",
  "purchase_count": 0,
  "category_count": 17
}
```

---

## 11. Ручной запуск бота

```bash
cd ~/apps/purchase-tracker
source .venv/bin/activate
cd purchase_tracker_telegram_agent
purchase-agent-bot
```

После запуска открой Telegram и напиши боту:

```text
/start
```

Потом:

```text
/whoami
```

Скопируй Telegram id в `.env`:

```env
TELEGRAM_ALLOWED_USER_IDS=123456789
```

Перезапусти бота.

---

## 12. Установка как systemd user service

Создай директорию для пользовательских systemd-сервисов:

```bash
mkdir -p ~/.config/systemd/user
```

Создай service-файл:

```bash
nano ~/.config/systemd/user/purchase-agent-bot.service
```

Вставь:

```ini
[Unit]
Description=Purchase Tracker Telegram Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/vityakarpenko2016/apps/purchase-tracker/purchase_tracker_telegram_agent
EnvironmentFile=/home/vityakarpenko2016/apps/purchase-tracker/purchase_tracker_telegram_agent/.env
ExecStart=/home/vityakarpenko2016/apps/purchase-tracker/.venv/bin/purchase-agent-bot
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

---

## 13. Запуск systemd-сервиса

```bash
systemctl --user daemon-reload
systemctl --user enable purchase-agent-bot
systemctl --user start purchase-agent-bot
```

Проверить статус:

```bash
systemctl --user status purchase-agent-bot
```

Посмотреть логи:

```bash
journalctl --user -u purchase-agent-bot -f
```

---

## 14. Чтобы сервис работал после выхода из SSH

Включи linger для пользователя:

```bash
sudo loginctl enable-linger "$USER"
```

Проверь:

```bash
loginctl show-user "$USER" | grep Linger
```

Ожидаемо:

```text
Linger=yes
```

---

## 15. Обновление исходников из GitHub

Останови сервис:

```bash
systemctl --user stop purchase-agent-bot
```

Обнови MCP-сервер:

```bash
cd ~/apps/purchase-tracker/purchase_tracker_mcp_project
git pull
```

Обнови агента:

```bash
cd ~/apps/purchase-tracker/purchase_tracker_telegram_agent
git pull
```

Переустанови оба проекта в editable-режиме:

```bash
cd ~/apps/purchase-tracker
source .venv/bin/activate

cd purchase_tracker_mcp_project
pip install -e .

cd ../purchase_tracker_telegram_agent
pip install -e .
```

Проверь связность:

```bash
purchase-agent-smoke
```

Запусти сервис:

```bash
systemctl --user start purchase-agent-bot
```

Проверь логи:

```bash
journalctl --user -u purchase-agent-bot -f
```

---

## 16. Резервная копия SQLite-БД

По умолчанию БД лежит здесь:

```text
/home/ubuntu/.purchase_tracker_mcp/purchases.sqlite3
```

Ручной бэкап:

```bash
mkdir -p ~/backups/purchase-tracker
cp ~/.purchase_tracker_mcp/purchases.sqlite3 ~/backups/purchase-tracker/purchases_$(date +%Y-%m-%d_%H-%M-%S).sqlite3
```

Также можно вызвать команду в Telegram:

```text
/backup
```

---

## 17. Основные команды бота

```text
/start — стартовое сообщение
/help — помощь
/whoami — показать Telegram user id
/health — проверить MCP и БД
/recent 10 — последние 10 покупок
/today — расходы за сегодня
/month — отчёт за текущий месяц
/categories — категории
/backup — резервная копия БД
/reset_context — сбросить контекст диалога
```

---

## 18. Примеры сообщений

```text
кофе 250
пятёрочка 1300 продукты
вчера такси 740
озон 2490 одежда
мак 860 кафе
удали последнюю
исправь последнюю на 270
сколько я потратила сегодня
покажи расходы по категориям за месяц
```

---

## 19. Важные замечания

### MCP не запускается отдельно

Не нужно делать отдельный systemd-сервис для `purchase-tracker-mcp`.

Агент сам запускает MCP-сервер через:

```env
MCP_SERVER_COMMAND=/home/ubuntu/apps/purchase-tracker/.venv/bin/purchase-tracker-mcp
MCP_SERVER_ARGS=
```

### Один virtualenv для двух проектов

Правильная схема:

```text
~/apps/purchase-tracker/
├── .venv/
├── purchase_tracker_mcp_project/
└── purchase_tracker_telegram_agent/
```

В `.venv` должны быть установлены оба проекта:

```bash
pip install -e ~/apps/purchase-tracker/purchase_tracker_mcp_project
pip install -e ~/apps/purchase-tracker/purchase_tracker_telegram_agent
```

### Безопасность

- Не коммить `.env`.
- Не публиковать Telegram token.
- Не публиковать LLM API key.
- Обязательно заполни `TELEGRAM_ALLOWED_USER_IDS`.
- Не открывай MCP-сервер наружу в интернет.
- SQLite-БД храни в домашней директории пользователя сервера.

---

## 20. Диагностика проблем

### `purchase-tracker-mcp: command not found`

Активируй virtualenv:

```bash
cd ~/apps/purchase-tracker
source .venv/bin/activate
```

Проверь установку MCP:

```bash
pip install -e ./purchase_tracker_mcp_project
which purchase-tracker-mcp
```

### `purchase-agent-bot: command not found`

```bash
cd ~/apps/purchase-tracker
source .venv/bin/activate
pip install -e ./purchase_tracker_telegram_agent
which purchase-agent-bot
```

### Бот не отвечает

Проверь статус:

```bash
systemctl --user status purchase-agent-bot
```

Проверь логи:

```bash
journalctl --user -u purchase-agent-bot -n 100
```

Проверь `.env`:

```bash
cd ~/apps/purchase-tracker/purchase_tracker_telegram_agent
cat .env
```

Особенно проверь:

```env
TELEGRAM_BOT_TOKEN=
LLM_API_KEY=
YANDEX_FOLDER_ID=
MCP_SERVER_COMMAND=
PURCHASE_DB_PATH=
```

### Ошибка доступа к боту

Напиши боту:

```text
/whoami
```

Скопируй id в `.env`:

```env
TELEGRAM_ALLOWED_USER_IDS=123456789
```

Перезапусти сервис:

```bash
systemctl --user restart purchase-agent-bot
```

### Смотреть логи в реальном времени

```bash
journalctl --user -u purchase-agent-bot -f
```
