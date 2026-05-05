from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from purchase_agent.config import Settings
from purchase_agent.llm_client import PurchaseAgent
from purchase_agent.mcp_client import PurchaseMCPClient

logger = logging.getLogger(__name__)
TELEGRAM_LIMIT = 3900


class PurchaseTelegramBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mcp_client = PurchaseMCPClient(settings)
        self.agent = PurchaseAgent(settings, self.mcp_client)
        self.bot = Bot(token=settings.telegram_bot_token)
        self.dp = Dispatcher()
        self._register_handlers()

    async def start(self) -> None:
        await self.mcp_client.connect()
        await self.dp.start_polling(self.bot)

    async def close(self) -> None:
        await self.mcp_client.close()
        await self.bot.session.close()

    def _register_handlers(self) -> None:
        self.dp.message.register(self.cmd_start, Command("start"))
        self.dp.message.register(self.cmd_help, Command("help"))
        self.dp.message.register(self.cmd_whoami, Command("whoami"))
        self.dp.message.register(self.cmd_health, Command("health"))
        self.dp.message.register(self.cmd_recent, Command("recent"))
        self.dp.message.register(self.cmd_today, Command("today"))
        self.dp.message.register(self.cmd_month, Command("month"))
        self.dp.message.register(self.cmd_categories, Command("categories"))
        self.dp.message.register(self.cmd_backup, Command("backup"))
        self.dp.message.register(self.cmd_purge_all_data, Command("purge_all_data"))
        self.dp.message.register(self.cmd_reset_context, Command("reset_context"))
        self.dp.message.register(self.handle_text, F.text)

    def has_access(self, message: Message) -> bool:
        if not self.settings.telegram_allowed_user_ids:
            return True
        user = message.from_user
        return bool(user and user.id in self.settings.telegram_allowed_user_ids)

    async def reject_if_needed(self, message: Message) -> bool:
        if self.has_access(message):
            return False
        user_id = message.from_user.id if message.from_user else "неизвестен"
        await message.answer(
            "Нет доступа к этому боту. "
            f"Твой Telegram id: {user_id}. Добавь его в TELEGRAM_ALLOWED_USER_IDS."
        )
        return True

    async def cmd_start(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        await message.answer(
            "Привет! Я бот для учёта покупок.\n\n"
            "Пиши обычным текстом, например:\n"
            "• кофе 250\n"
            "• пятёрочка 1300 продукты\n"
            "• вчера такси 740\n"
            "• сколько ушло на кафе за месяц\n"
            "• удали последнюю покупку\n\n"
            "Команды: /help, /recent, /today, /month, /categories, /backup, /whoami."
        )

    async def cmd_help(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        await message.answer(
            "Что можно писать:\n"
            "• Записать покупку: 'кофе 250', 'лента 3200 продукты', 'вчера аптека 560'.\n"
            "• Исправить: 'исправь последнюю на 270', 'перенеси последнюю в кафе'.\n"
            "• Удалить: 'удали последнюю'.\n"
            "• Аналитика: 'сколько потратила сегодня', 'траты по категориям за месяц'.\n\n"
            "Команды:\n"
            "/recent 10 — последние покупки.\n"
            "/today — расходы за сегодня по категориям.\n"
            "/month — отчёт по лимитам за текущий месяц.\n"
            "/categories — категории.\n"
            "/backup — сделать резервную копию БД.\n"
            "/reset_context — сбросить диалоговый контекст, не трогая БД.\n"
            "/health — проверить MCP и БД.\n"
            "/whoami — показать твой Telegram id."
        )

    async def cmd_whoami(self, message: Message) -> None:
        user = message.from_user
        if user is None:
            await message.answer("Не вижу Telegram user id.")
            return
        await message.answer(f"Твой Telegram id: {user.id}")

    async def cmd_health(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        result = await self.mcp_client.call_tool("health", {}, user_id=get_message_user_id(message))
        payload = result.get("payload") or result
        await message.answer(format_health(payload))

    async def cmd_recent(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        limit = parse_first_int_arg(message.text or "", default=10, min_value=1, max_value=50)
        result = await self.mcp_client.call_tool(
            "list_purchases",
            {"limit": limit, "sort": "spent_at_desc"},
            user_id=get_message_user_id(message),
        )
        payload = result.get("payload") or result
        await answer_long(message, format_purchases_list(payload, title=f"Последние покупки: {limit}"))

    async def cmd_today(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        now = datetime.now(ZoneInfo(self.settings.agent_timezone))
        today = now.date().isoformat()
        result = await self.mcp_client.call_tool(
            "get_summary",
            {
                "start_date": today,
                "end_date": today,
                "group_by": "category",
                "currency": self.settings.agent_default_currency,
            },
            user_id=get_message_user_id(message),
        )
        payload = result.get("payload") or result
        await answer_long(message, format_summary(payload, title=f"Сегодня, {today}"))

    async def cmd_month(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        now = datetime.now(ZoneInfo(self.settings.agent_timezone))
        result = await self.mcp_client.call_tool(
            "monthly_budget_report",
            {
                "year": now.year,
                "month": now.month,
                "currency": self.settings.agent_default_currency,
            },
            user_id=get_message_user_id(message),
        )
        payload = result.get("payload") or result
        await answer_long(message, format_monthly_budget(payload))

    async def cmd_categories(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        result = await self.mcp_client.call_tool(
            "list_categories",
            {"include_usage": True},
            user_id=get_message_user_id(message),
        )
        payload = result.get("payload") or result
        await answer_long(message, format_categories(payload))

    async def cmd_backup(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        result = await self.mcp_client.call_tool(
            "backup_database",
            {},
            user_id=get_message_user_id(message),
        )
        payload = result.get("payload") or result
        backup_path = payload.get("backup_path") if isinstance(payload, dict) else None
        if backup_path:
            await message.answer(f"Готово, резервная копия создана:\n{backup_path}")
        else:
            await message.answer(f"Не смогла создать резервную копию:\n{payload}")

    async def cmd_purge_all_data(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        if (message.text or "").strip() != "/purge_all_data DELETE ALL PURCHASE DATA":
            await message.answer(
                "Опасная команда отклонена. Используй точную ручную команду:\n"
                "/purge_all_data DELETE ALL PURCHASE DATA"
            )
            return
        result = await self.mcp_client.call_tool(
            "purge_all_data",
            {"confirm": "DELETE ALL PURCHASE DATA"},
        )
        payload = result.get("payload") or result
        await answer_long(message, format_purge_result(payload))

    async def cmd_reset_context(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        self.agent.reset_history(message.chat.id)
        await message.answer("Контекст диалога сброшен. База покупок не изменялась.")

    async def handle_text(self, message: Message) -> None:
        if await self.reject_if_needed(message):
            return
        if not message.text or message.text.startswith("/"):
            return

        async with ChatActionSender.typing(bot=self.bot, chat_id=message.chat.id):
            answer = await self.agent.handle_user_text(
                message.chat.id,
                message.text,
                user_id=get_message_user_id(message),
            )
        await answer_long(message, answer)


def get_message_user_id(message: Message) -> int | None:
    user = message.from_user
    return user.id if user else None


async def answer_long(message: Message, text: str) -> None:
    if len(text) <= TELEGRAM_LIMIT:
        await message.answer(text)
        return
    parts = split_long_text(text, TELEGRAM_LIMIT)
    for part in parts:
        await message.answer(part)


async def run_bot(settings: Settings) -> None:
    app = PurchaseTelegramBot(settings)
    try:
        await app.start()
    finally:
        await app.close()


def parse_first_int_arg(text: str, *, default: int, min_value: int, max_value: int) -> int:
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return default
    try:
        value = int(parts[1].strip())
    except ValueError:
        return default
    return min(max(value, min_value), max_value)


def split_long_text(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = text
    while len(current) > limit:
        cut = current.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(current[:cut].strip())
        current = current[cut:].strip()
    if current:
        chunks.append(current)
    return chunks


def format_money(value: Any, currency: str | None = None) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return str(value)
    text = f"{amount:,.2f}".replace(",", " ").replace(".00", "")
    return f"{text} {currency}".strip() if currency else text


def format_health(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return f"MCP/БД недоступны: {payload}"
    return (
        "MCP и БД доступны.\n"
        f"Покупок: {payload.get('purchase_count')}\n"
        f"Категорий: {payload.get('category_count')}\n"
        f"БД: {payload.get('db_path')}"
    )


def format_purchase(item: dict[str, Any]) -> str:
    merchant = item.get("merchant") or "без магазина"
    category = item.get("category") or "Без категории"
    amount = format_money(item.get("amount"), item.get("currency"))
    spent_at = str(item.get("spent_at") or "")[:16].replace("T", " ")
    description = item.get("description") or ""
    tail = f" — {description}" if description else ""
    return f"#{item.get('id')} · {amount} · {category} · {merchant} · {spent_at}{tail}"


def format_purchases_list(payload: dict[str, Any], *, title: str) -> str:
    if not payload.get("ok"):
        return f"Не удалось получить покупки: {payload}"
    items = payload.get("items") or []
    total_count = payload.get("total_count", payload.get("total"))
    if not items:
        return f"{title}\nПокупок не найдено."
    lines = [title]
    if total_count is not None:
        lines.append(f"Всего найдено: {total_count}")
    lines.extend(format_purchase(item) for item in items)
    return "\n".join(lines)


def format_summary(payload: dict[str, Any], *, title: str) -> str:
    if not payload.get("ok"):
        return f"Не удалось получить статистику: {payload}"
    totals = payload.get("totals") or {}
    items = payload.get("items") or []
    currency = payload.get("currency") or ""
    lines = [title]
    lines.append(f"Всего: {format_money(totals.get('total_amount'), currency)}")
    lines.append(f"Покупок: {totals.get('purchase_count', 0)}")
    if items:
        lines.append("")
        for item in items[:10]:
            group = item.get("group_value") or "Без значения"
            item_currency = item.get("currency") or currency
            lines.append(
                f"• {group}: {format_money(item.get('total_amount'), item_currency)} "
                f"({item.get('purchase_count')} шт.)"
            )
    return "\n".join(lines)


def format_monthly_budget(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return f"Не удалось получить месячный отчёт: {payload}"
    period = payload.get("period")
    currency = payload.get("currency") or ""
    total_spent = payload.get("total_spent")
    total_limit = payload.get("total_limit")
    items = payload.get("items") or []
    lines = [f"Отчёт за {period}"]
    lines.append(f"Потрачено: {format_money(total_spent, currency)}")
    if total_limit:
        lines.append(f"Лимиты: {format_money(total_limit, currency)}")
    if items:
        lines.append("")
        for item in items[:12]:
            category = item.get("category")
            spent = format_money(item.get("spent"), currency)
            limit = item.get("monthly_limit")
            if limit is None:
                lines.append(f"• {category}: {spent}")
            else:
                status = item.get("status")
                remaining = format_money(item.get("remaining"), currency)
                lines.append(f"• {category}: {spent} / {format_money(limit, currency)} · остаток {remaining} · {status}")
    return "\n".join(lines)


def format_purge_result(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return f"Не удалось полностью очистить данные: {payload}"
    return "Полная очистка данных выполнена."


def format_categories(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return f"Не удалось получить категории: {payload}"
    items = payload.get("items") or []
    if not items:
        return "Категорий пока нет."
    lines = ["Категории:"]
    for item in items[:80]:
        name = item.get("name")
        limit = item.get("monthly_limit")
        count = item.get("purchase_count")
        total = item.get("total_amount")
        limit_part = f", лимит {format_money(limit)}" if limit is not None else ""
        usage_part = f", покупок {count}, всего {format_money(total)}" if count is not None else ""
        lines.append(f"• {name}{limit_part}{usage_part}")
    return "\n".join(lines)
