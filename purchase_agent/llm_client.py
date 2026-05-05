from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

from purchase_agent.config import Settings
from purchase_agent.mcp_client import PurchaseMCPClient, result_to_text, safe_call_tool
from purchase_agent.prompts import JSON_TOOL_MODE_INSTRUCTIONS, build_system_prompt

logger = logging.getLogger(__name__)


class ConversationStore:
    """Small in-memory history for Telegram chats."""

    def __init__(self, max_turns: int = 10) -> None:
        self.max_messages = max(2, max_turns * 2)
        self._storage: dict[int, list[dict[str, str]]] = {}

    def get(self, chat_id: int) -> list[dict[str, str]]:
        return list(self._storage.get(chat_id, []))

    def add_user(self, chat_id: int, text: str) -> None:
        self._append(chat_id, {"role": "user", "content": text})

    def add_assistant(self, chat_id: int, text: str) -> None:
        self._append(chat_id, {"role": "assistant", "content": text})

    def reset(self, chat_id: int) -> None:
        self._storage.pop(chat_id, None)

    def _append(self, chat_id: int, message: dict[str, str]) -> None:
        history = self._storage.setdefault(chat_id, [])
        history.append(message)
        del history[:-self.max_messages]


class PurchaseAgent:
    def __init__(self, settings: Settings, mcp_client: PurchaseMCPClient) -> None:
        self._settings = settings
        self._mcp = mcp_client
        self._client = create_openai_client(settings)
        self.history = ConversationStore(max_turns=settings.agent_history_turns)

    async def handle_user_text(self, chat_id: int, text: str, *, user_id: int | str | None = None) -> str:
        logger.debug(
            "Received user message chat_id=%s user_id=%s text=%s",
            chat_id,
            user_id,
            _truncate_for_log(text),
        )
        self.history.add_user(chat_id, text)
        messages = await self._base_messages(chat_id, text)

        try:
            if self._settings.llm_tool_mode == "json":
                answer = await self._run_json_tool_loop(messages, user_id=user_id)
            elif self._settings.llm_tool_mode == "native":
                answer = await self._run_native_tool_loop(messages, user_id=user_id)
            else:
                answer = await self._run_auto_tool_loop(messages, user_id=user_id)
        except Exception as exc:
            logger.exception("Agent failed")
            answer = (
                "Не смогла обработать сообщение из-за ошибки. "
                f"Техническая причина: {type(exc).__name__}: {exc}"
            )

        self.history.add_assistant(chat_id, answer)
        return answer

    async def _execute_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        user_id: int | str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        logger.debug(
            "Executing tool call name=%s arguments=%s",
            tool_name,
            _to_json_for_log(arguments),
        )
        result = await safe_call_tool(self._mcp, tool_name, arguments, user_id=user_id)
        logger.debug(
            "Tool result name=%s result=%s",
            tool_name,
            _to_json_for_log(result),
        )
        verified_result = await self._verify_tool_result(tool_name, result, user_id=user_id)
        if verified_result is not result:
            logger.debug(
                "Tool result verified name=%s verified_result=%s",
                tool_name,
                _to_json_for_log(verified_result),
            )
        self._log_write_tool_success(tool_name, verified_result, user_id=user_id)
        forced_reply = _build_forced_tool_reply(tool_name, verified_result)
        return verified_result, forced_reply

    async def _verify_tool_result(
        self,
        tool_name: str,
        result: dict[str, Any],
        *,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        if tool_name != "add_purchase":
            return result
        payload = result.get("payload")
        purchase_id = _extract_purchase_id(payload)
        if purchase_id is None:
            return result
        verification_result = await safe_call_tool(
            self._mcp,
            "get_purchase",
            {"id": purchase_id},
            user_id=user_id,
        )
        verification_payload = verification_result.get("payload")
        if not _is_verified_purchase_result(verification_result, verification_payload, purchase_id):
            return {
                "ok": False,
                "error": "Запись не прошла post-write verification через get_purchase.",
                "tool_name": tool_name,
                "payload": payload,
                "verification": verification_result,
            }
        merged = dict(result)
        merged["verification"] = verification_result
        return merged

    def _log_write_tool_success(
        self,
        tool_name: str,
        result: dict[str, Any],
        *,
        user_id: int | str | None = None,
    ) -> None:
        if tool_name not in WRITE_TOOL_NAMES:
            return
        payload = result.get("payload")
        if not _is_successful_write_result(tool_name, result, payload):
            return
        logger.info(
            "Confirmed write tool success tool=%s telegram_user_id=%s entity_id=%s details=%s",
            tool_name,
            user_id,
            _extract_entity_id(payload),
            _to_json_for_log(_build_log_details(payload), limit=500),
        )

    def reset_history(self, chat_id: int) -> None:
        self.history.reset(chat_id)

    async def _base_messages(self, chat_id: int, user_text: str) -> list[dict[str, Any]]:
        system_prompt = build_system_prompt(
            timezone=self._settings.agent_timezone,
            default_currency=self._settings.agent_default_currency,
        )
        category_hint = await self._build_category_hint(user_text)
        if category_hint:
            system_prompt = f"{system_prompt}\n\n{category_hint}"
        return [{"role": "system", "content": system_prompt}, *self.history.get(chat_id)]

    async def _build_category_hint(self, user_text: str) -> str:
        normalized_text = user_text.strip().lower()
        if not normalized_text:
            return ""
        if not _looks_like_category_sensitive_request(normalized_text):
            return ""

        result = await safe_call_tool(
            self._mcp,
            "list_categories",
            {"include_usage": True},
        )
        payload = result.get("payload") or {}
        if not isinstance(payload, dict) or not payload.get("ok"):
            return ""
        items = payload.get("items") or []
        if not items:
            return ""

        category_names = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                category_names.append(name)
        if not category_names:
            return ""

        categories_json = json.dumps(category_names, ensure_ascii=False)
        return (
            "Список уже существующих категорий пользователя: "
            f"{categories_json}. "
            "При записи покупки сначала выбери категорию только из этого списка. "
            "Если формулировка пользователя похожа на одну из этих категорий по смыслу, используй именно существующую категорию. "
            "Не создавай новую категорию без явной команды пользователя."
        )

    async def _run_auto_tool_loop(
        self,
        messages: list[dict[str, Any]],
        *,
        user_id: int | str | None = None,
    ) -> str:
        try:
            return await self._run_native_tool_loop(messages, user_id=user_id)
        except Exception as exc:
            logger.warning("Native tool mode failed, falling back to JSON tool mode: %s", exc)
            fallback_messages = _strip_tool_messages(messages)
            return await self._run_json_tool_loop(fallback_messages, user_id=user_id)

    async def _run_native_tool_loop(
        self,
        messages: list[dict[str, Any]],
        *,
        user_id: int | str | None = None,
    ) -> str:
        tools = await self._mcp.list_tools_openai_schema()
        working_messages = [dict(message) for message in messages]
        logger.debug(
            "Starting native tool loop user_id=%s messages=%s tools=%s",
            user_id,
            _summarize_messages_for_log(working_messages),
            _summarize_native_tools_for_log(tools),
        )

        for _ in range(self._settings.llm_max_tool_iterations):
            response = await self._client.chat.completions.create(
                model=self._settings.llm_model,
                messages=working_messages,
                tools=tools,
                tool_choice="auto",
                temperature=self._settings.llm_temperature,
                max_tokens=self._settings.llm_max_tokens,
            )
            message = response.choices[0].message
            content = message.content or ""
            tool_calls = list(message.tool_calls or [])
            logger.debug(
                "Native LLM response content=%s tool_calls=%s",
                _truncate_for_log(content),
                _summarize_tool_calls_for_log(tool_calls),
            )

            if not tool_calls:
                final_text = content.strip() or "Готово."
                logger.debug("Native tool loop finished with final answer=%s", _truncate_for_log(final_text))
                return final_text

            working_messages.append(_assistant_message_with_tool_calls(content, tool_calls))

            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}

                tool_result, forced_reply = await self._execute_tool_call(
                    tool_name,
                    arguments,
                    user_id=user_id,
                )
                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": result_to_text(tool_result),
                    }
                )
                if forced_reply is not None:
                    return forced_reply

        return "Я выполнила несколько действий, но не смогла компактно сформировать финальный ответ."

    async def _run_json_tool_loop(
        self,
        messages: list[dict[str, Any]],
        *,
        user_id: int | str | None = None,
    ) -> str:
        tools = await self._mcp.list_tools_for_prompt()
        tools_catalog = json.dumps(tools, ensure_ascii=False, indent=2)
        logger.debug(
            "Starting JSON tool loop user_id=%s messages=%s tools=%s",
            user_id,
            _summarize_messages_for_log(messages),
            _summarize_prompt_tools_for_log(tools),
        )
        system_with_json_protocol = dict(messages[0])
        system_with_json_protocol["content"] = (
            system_with_json_protocol["content"]
            + "\n\n"
            + JSON_TOOL_MODE_INSTRUCTIONS
            + "\n\nКаталог MCP tools:\n"
            + tools_catalog
        )

        working_messages = [system_with_json_protocol, *messages[1:]]

        for _ in range(self._settings.llm_max_tool_iterations):
            response = await self._client.chat.completions.create(
                model=self._settings.llm_model,
                messages=working_messages,
                temperature=self._settings.llm_temperature,
                max_tokens=self._settings.llm_max_tokens,
            )
            raw_text = response.choices[0].message.content or ""
            command = parse_json_command(raw_text)
            logger.debug(
                "JSON tool loop raw response=%s parsed_command=%s",
                _truncate_for_log(raw_text),
                _to_json_for_log(command),
            )

            if command is None:
                final_text = raw_text.strip() or "Не смогла разобрать ответ модели."
                logger.debug("JSON tool loop finished without command final answer=%s", _truncate_for_log(final_text))
                return final_text

            command_type = command.get("type")
            if command_type == "final":
                final_text = str(command.get("message") or "Готово.").strip()
                logger.debug("JSON tool loop finished with final answer=%s", _truncate_for_log(final_text))
                return final_text

            if command_type == "tool_call":
                calls = [
                    {
                        "tool_name": command.get("tool_name"),
                        "arguments": command.get("arguments") or {},
                    }
                ]
            elif command_type == "tool_calls":
                raw_calls = command.get("calls") or []
                calls = raw_calls if isinstance(raw_calls, list) else []
            else:
                return "Не смогла понять, какое действие нужно выполнить."

            tool_results = []
            for call in calls:
                tool_name = str(call.get("tool_name") or "").strip()
                arguments = call.get("arguments") or {}
                if not tool_name:
                    tool_results.append({"ok": False, "error": "tool_name пустой"})
                    continue
                if not isinstance(arguments, dict):
                    arguments = {}

                result, forced_reply = await self._execute_tool_call(
                    tool_name,
                    arguments,
                    user_id=user_id,
                )
                tool_results.append(
                    {"tool_name": tool_name, "arguments": arguments, "result": result.get("payload", result)}
                )
                if forced_reply is not None:
                    return forced_reply

            working_messages.append({"role": "assistant", "content": json.dumps(command, ensure_ascii=False)})
            working_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Результаты MCP tools:\n"
                        + json.dumps(tool_results, ensure_ascii=False, default=str)
                        + "\nВерни финальный ответ пользователю в JSON формате type=final."
                    ),
                }
            )

        return "Я выполнила действия, но не смогла завершить ответ за допустимое число шагов."


def create_openai_client(settings: Settings) -> AsyncOpenAI:
    default_headers = None
    api_key_for_sdk = settings.llm_api_key

    if settings.llm_auth_header_mode == "api-key":
        default_headers = {"Authorization": f"Api-Key {settings.llm_api_key}"}
        api_key_for_sdk = settings.llm_api_key

    return AsyncOpenAI(
        api_key=api_key_for_sdk,
        base_url=settings.llm_base_url,
        project=settings.llm_project,
        default_headers=default_headers,
    )


def parse_json_command(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None

    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _assistant_message_with_tool_calls(content: str, tool_calls: list[Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments or "{}",
                },
            }
            for tool_call in tool_calls
        ],
    }


def _strip_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "tool":
            continue
        clean = {"role": message.get("role"), "content": message.get("content", "")}
        stripped.append(clean)
    return stripped


def _truncate_for_log(value: Any, limit: int = 1200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _to_json_for_log(value: Any, limit: int = 1200) -> str:
    return _truncate_for_log(value, limit=limit)


def _summarize_messages_for_log(messages: list[dict[str, Any]]) -> str:
    summary = []
    for message in messages:
        item = {
            "role": message.get("role"),
            "content": _truncate_for_log(message.get("content", ""), limit=300),
        }
        if message.get("tool_calls"):
            item["tool_calls"] = message.get("tool_calls")
        if message.get("name"):
            item["name"] = message.get("name")
        summary.append(item)
    return _to_json_for_log(summary)


def _summarize_tool_calls_for_log(tool_calls: list[Any]) -> str:
    payload = []
    for tool_call in tool_calls:
        payload.append(
            {
                "id": getattr(tool_call, "id", None),
                "name": getattr(getattr(tool_call, "function", None), "name", None),
                "arguments": _truncate_for_log(
                    getattr(getattr(tool_call, "function", None), "arguments", "{}"),
                    limit=500,
                ),
            }
        )
    return _to_json_for_log(payload)


def _summarize_native_tools_for_log(tools: list[dict[str, Any]]) -> str:
    payload = [tool.get("function", {}).get("name") for tool in tools]
    return _to_json_for_log(payload, limit=600)


def _summarize_prompt_tools_for_log(tools: list[dict[str, Any]]) -> str:
    payload = [tool.get("name") for tool in tools]
    return _to_json_for_log(payload, limit=600)


WRITE_TOOL_NAMES = frozenset(
    {
        "add_purchase",
        "update_purchase",
        "delete_purchase",
        "upsert_category",
        "rename_category",
        "delete_category",
        "import_purchases_csv",
    }
)


SUCCESS_MESSAGE_BY_TOOL = {
    "add_purchase": "Запись в БД подтверждена.",
    "update_purchase": "Изменения в БД подтверждены.",
    "delete_purchase": "Удаление из БД подтверждено.",
    "upsert_category": "Изменение категории в БД подтверждено.",
    "rename_category": "Переименование категории в БД подтверждено.",
    "delete_category": "Удаление категории в БД подтверждено.",
    "import_purchases_csv": "Импорт в БД подтверждён.",
}


FAILURE_MESSAGE_BY_TOOL = {
    "add_purchase": "Не удалось записать покупку в БД.",
    "update_purchase": "Не удалось изменить покупку в БД.",
    "delete_purchase": "Не удалось удалить покупку из БД.",
    "upsert_category": "Не удалось сохранить категорию в БД.",
    "rename_category": "Не удалось переименовать категорию в БД.",
    "delete_category": "Не удалось удалить категорию из БД.",
    "import_purchases_csv": "Не удалось импортировать покупки в БД.",
}


SUCCESS_KEYS_BY_TOOL = {
    "add_purchase": ("id", "purchase_id", "amount", "spent_at"),
    "update_purchase": ("id", "purchase_id", "updated"),
    "delete_purchase": ("id", "purchase_id", "deleted"),
    "upsert_category": ("id", "name", "category", "created", "updated"),
    "rename_category": ("id", "name", "category", "updated"),
    "delete_category": ("id", "name", "category", "deleted"),
    "import_purchases_csv": ("imported_count",),
}


ERROR_KEYS = ("error", "errors", "detail", "message")


def _looks_like_category_sensitive_request(text: str) -> bool:
    keywords = (
        "катег",
        "кредит",
        "кредитк",
        "рассроч",
        "лимит",
        "плат",
        "долг",
    )
    return any(keyword in text for keyword in keywords)


def _build_forced_tool_reply(tool_name: str, result: dict[str, Any]) -> str | None:
    if tool_name not in WRITE_TOOL_NAMES:
        return None
    payload = result.get("payload")
    if _is_successful_write_result(tool_name, result, payload):
        return _build_success_tool_reply(tool_name, payload)
    return _build_failure_tool_reply(tool_name, result, payload)


def _is_successful_write_result(tool_name: str, result: dict[str, Any], payload: Any) -> bool:
    if not result.get("ok"):
        return False
    if payload is None or not isinstance(payload, dict):
        return False
    if payload.get("ok") is False:
        return False
    expected_keys = SUCCESS_KEYS_BY_TOOL.get(tool_name, ())
    return any(payload.get(key) not in (None, "", False) for key in expected_keys)


def _build_success_tool_reply(tool_name: str, payload: Any) -> str:
    message = SUCCESS_MESSAGE_BY_TOOL.get(tool_name, "Операция в БД подтверждена.")
    details = _format_tool_payload_details(payload)
    return f"{message}\n{details}" if details else message


def _build_failure_tool_reply(tool_name: str, result: dict[str, Any], payload: Any) -> str:
    message = FAILURE_MESSAGE_BY_TOOL.get(tool_name, "Операция с БД завершилась ошибкой.")
    reason = _extract_error_text(result, payload)
    return f"{message}\nПричина: {reason}" if reason else message


def _format_tool_payload_details(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    preferred_keys = (
        "id",
        "purchase_id",
        "category",
        "name",
        "deleted",
        "updated",
        "created",
        "imported_count",
    )
    parts = []
    for key in preferred_keys:
        value = payload.get(key)
        if value in (None, "", False):
            continue
        parts.append(f"{key}={value}")
    return ", ".join(parts)


def _extract_error_text(result: dict[str, Any], payload: Any) -> str:
    verification = result.get("verification")
    if isinstance(verification, dict):
        verification_payload = verification.get("payload")
        if isinstance(verification_payload, dict):
            for key in ERROR_KEYS:
                value = verification_payload.get(key)
                if value not in (None, ""):
                    return str(value)
    if isinstance(payload, dict):
        for key in ERROR_KEYS:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    for key in ERROR_KEYS:
        value = result.get(key)
        if value not in (None, ""):
            return str(value)
    if isinstance(payload, dict) and payload:
        return json.dumps(payload, ensure_ascii=False, default=str)
    return ""


def _extract_purchase_id(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "purchase_id"):
        value = payload.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _is_verified_purchase_result(result: dict[str, Any], payload: Any, purchase_id: int) -> bool:
    if not result.get("ok"):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") is False:
        return False
    actual_id = _extract_purchase_id(payload)
    return actual_id == purchase_id


def _extract_entity_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "purchase_id", "name", "category"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _build_log_details(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    keys = (
        "id",
        "purchase_id",
        "amount",
        "currency",
        "spent_at",
        "category",
        "name",
        "deleted",
        "updated",
        "created",
        "imported_count",
    )
    return {key: payload.get(key) for key in keys if payload.get(key) not in (None, "", False)}
