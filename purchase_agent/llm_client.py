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
        messages = await self._base_messages(chat_id, text, user_id=user_id)

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
        status_prefix = _build_write_tool_status_prefix(tool_name, verified_result)
        return verified_result, status_prefix

    async def _verify_tool_result(
        self,
        tool_name: str,
        result: dict[str, Any],
        *,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        payload = result.get("payload")
        if tool_name == "add_purchase":
            return await self._verify_add_purchase_result(result, payload, user_id=user_id)
        if tool_name == "update_purchase":
            return await self._verify_update_purchase_result(result, payload, user_id=user_id)
        if tool_name == "delete_purchase":
            return await self._verify_delete_purchase_result(result, payload, user_id=user_id)
        if tool_name == "upsert_category":
            return await self._verify_upsert_category_result(result, payload, user_id=user_id)
        if tool_name == "rename_category":
            return await self._verify_rename_category_result(result, payload, user_id=user_id)
        if tool_name == "delete_category":
            return await self._verify_delete_category_result(result, payload, user_id=user_id)
        if tool_name == "import_purchases_csv":
            return await self._verify_import_result(result, payload, user_id=user_id)
        return result

    async def _verify_add_purchase_result(
        self,
        result: dict[str, Any],
        payload: Any,
        *,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        purchase_id = _extract_purchase_id(payload)
        if purchase_id is None:
            return result
        verification_result = await safe_call_tool(
            self._mcp,
            "get_purchase",
            {"purchase_id": purchase_id},
            user_id=user_id,
        )
        verification_payload = verification_result.get("payload")
        if not _is_verified_purchase_result(verification_result, verification_payload, purchase_id, user_id=user_id):
            return _build_verification_failure(
                tool_name="add_purchase",
                error="Запись не прошла post-write verification через get_purchase.",
                payload=payload,
                verification=verification_result,
            )
        return _merge_verification(result, verification_result)

    async def _verify_update_purchase_result(
        self,
        result: dict[str, Any],
        payload: Any,
        *,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        purchase_id = _extract_purchase_id(payload)
        if purchase_id is None:
            return result
        verification_result = await safe_call_tool(
            self._mcp,
            "get_purchase",
            {"purchase_id": purchase_id},
            user_id=user_id,
        )
        verification_payload = verification_result.get("payload")
        if not _is_verified_purchase_result(verification_result, verification_payload, purchase_id, user_id=user_id):
            return _build_verification_failure(
                tool_name="update_purchase",
                error="Изменение не прошло verification через get_purchase.",
                payload=payload,
                verification=verification_result,
            )
        return _merge_verification(result, verification_result)

    async def _verify_delete_purchase_result(
        self,
        result: dict[str, Any],
        payload: Any,
        *,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        purchase_id = _extract_purchase_id(payload)
        if purchase_id is None:
            return result
        verification_result = await safe_call_tool(
            self._mcp,
            "get_purchase",
            {"purchase_id": purchase_id},
            user_id=user_id,
        )
        verification_payload = verification_result.get("payload")
        if not _is_verified_deleted_purchase_result(verification_result, verification_payload):
            return _build_verification_failure(
                tool_name="delete_purchase",
                error="Удаление не прошло verification через get_purchase.",
                payload=payload,
                verification=verification_result,
            )
        return _merge_verification(result, verification_result)

    async def _verify_upsert_category_result(
        self,
        result: dict[str, Any],
        payload: Any,
        *,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        category_name = _extract_category_name(payload)
        if not category_name:
            return result
        verification_result = await safe_call_tool(
            self._mcp,
            "get_category",
            {"name": category_name},
            user_id=user_id,
        )
        verification_payload = verification_result.get("payload")
        if not _is_verified_category_present(verification_result, verification_payload, category_name, user_id=user_id):
            return _build_verification_failure(
                tool_name="upsert_category",
                error="Категория не прошла verification через get_category.",
                payload=payload,
                verification=verification_result,
            )
        return _merge_verification(result, verification_result)

    async def _verify_rename_category_result(
        self,
        result: dict[str, Any],
        payload: Any,
        *,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        old_name, new_name = _extract_rename_category_names(payload)
        if not old_name or not new_name:
            return result
        new_result = await safe_call_tool(self._mcp, "get_category", {"name": new_name}, user_id=user_id)
        old_result = await safe_call_tool(self._mcp, "get_category", {"name": old_name}, user_id=user_id)
        new_payload = new_result.get("payload")
        old_payload = old_result.get("payload")
        if not _is_verified_category_present(new_result, new_payload, new_name, user_id=user_id):
            return _build_verification_failure(
                tool_name="rename_category",
                error="Новая категория не найдена после rename.",
                payload=payload,
                verification={"new": new_result, "old": old_result},
            )
        if not _is_verified_category_absent(old_result, old_payload):
            return _build_verification_failure(
                tool_name="rename_category",
                error="Старая категория всё ещё существует после rename.",
                payload=payload,
                verification={"new": new_result, "old": old_result},
            )
        return _merge_verification(result, {"new": new_result, "old": old_result})

    async def _verify_delete_category_result(
        self,
        result: dict[str, Any],
        payload: Any,
        *,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        deleted_name, target_name = _extract_delete_category_names(payload)
        if not deleted_name or not target_name:
            return result
        deleted_result = await safe_call_tool(self._mcp, "get_category", {"name": deleted_name}, user_id=user_id)
        target_result = await safe_call_tool(self._mcp, "get_category", {"name": target_name}, user_id=user_id)
        deleted_payload = deleted_result.get("payload")
        target_payload = target_result.get("payload")
        if not _is_verified_category_absent(deleted_result, deleted_payload):
            return _build_verification_failure(
                tool_name="delete_category",
                error="Удалённая категория всё ещё существует после delete.",
                payload=payload,
                verification={"deleted": deleted_result, "target": target_result},
            )
        if not _is_verified_category_present(target_result, target_payload, target_name, user_id=user_id):
            return _build_verification_failure(
                tool_name="delete_category",
                error="Целевая категория для переноса не найдена после delete.",
                payload=payload,
                verification={"deleted": deleted_result, "target": target_result},
            )
        return _merge_verification(result, {"deleted": deleted_result, "target": target_result})

    async def _verify_import_result(
        self,
        result: dict[str, Any],
        payload: Any,
        *,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        imported_ids = _extract_imported_purchase_ids(payload)
        if not imported_ids:
            return result
        verification_results = []
        for purchase_id in imported_ids:
            verification_result = await safe_call_tool(
                self._mcp,
                "get_purchase",
                {"purchase_id": purchase_id},
                user_id=user_id,
            )
            verification_payload = verification_result.get("payload")
            verification_results.append(verification_result)
            if not _is_verified_purchase_result(
                verification_result,
                verification_payload,
                purchase_id,
                user_id=user_id,
            ):
                return _build_verification_failure(
                    tool_name="import_purchases_csv",
                    error=f"Импорт не прошёл verification для purchase_id={purchase_id}.",
                    payload=payload,
                    verification=verification_results,
                )
        return _merge_verification(result, verification_results)

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

    async def _base_messages(
        self,
        chat_id: int,
        user_text: str,
        *,
        user_id: int | str | None = None,
    ) -> list[dict[str, Any]]:
        system_prompt = build_system_prompt(
            timezone=self._settings.agent_timezone,
            default_currency=self._settings.agent_default_currency,
        )
        category_hint = await self._build_category_hint(user_text, user_id=user_id)
        if category_hint:
            system_prompt = f"{system_prompt}\n\n{category_hint}"
        return [{"role": "system", "content": system_prompt}, *self.history.get(chat_id)]

    async def _build_category_hint(
        self,
        user_text: str,
        *,
        user_id: int | str | None = None,
    ) -> str:
        normalized_text = user_text.strip().lower()
        if not normalized_text:
            return ""
        if not _looks_like_category_sensitive_request(normalized_text):
            return ""

        result = await safe_call_tool(
            self._mcp,
            "list_categories",
            {"include_usage": True},
            user_id=user_id,
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
                    working_messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Сначала покажи короткий статус операции одной строкой, затем пустую строку, "
                                "потом дай нормальный красивый ответ пользователю по-русски. "
                                f"Строго используй эту первую строку без изменений: {forced_reply}"
                            ),
                        }
                    )

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
                    tool_results.append(
                        {
                            "tool_name": "system_status",
                            "arguments": {},
                            "result": {"status_prefix": forced_reply},
                        }
                    )

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
    "add_purchase": "✅ Запись подтверждена",
    "update_purchase": "✅ Изменения подтверждены",
    "delete_purchase": "✅ Удаление подтверждено",
    "upsert_category": "✅ Категория сохранена",
    "rename_category": "✅ Категория переименована",
    "delete_category": "✅ Категория удалена",
    "import_purchases_csv": "✅ Импорт подтверждён",
}


FAILURE_MESSAGE_BY_TOOL = {
    "add_purchase": "❌ Не удалось записать покупку",
    "update_purchase": "❌ Не удалось изменить покупку",
    "delete_purchase": "❌ Не удалось удалить покупку",
    "upsert_category": "❌ Не удалось сохранить категорию",
    "rename_category": "❌ Не удалось переименовать категорию",
    "delete_category": "❌ Не удалось удалить категорию",
    "import_purchases_csv": "❌ Не удалось импортировать покупки",
}


SUCCESS_KEYS_BY_TOOL = {
    "add_purchase": ("id", "purchase_id", "amount", "spent_at"),
    "update_purchase": ("id", "purchase_id", "changed"),
    "delete_purchase": ("id", "purchase_id", "changed"),
    "upsert_category": ("category",),
    "rename_category": ("changed", "new_name"),
    "delete_category": ("deleted_category", "move_purchases_to"),
    "import_purchases_csv": ("imported_count", "imported_purchase_ids"),
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


def _build_write_tool_status_prefix(tool_name: str, result: dict[str, Any]) -> str | None:
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
    message = SUCCESS_MESSAGE_BY_TOOL.get(tool_name, "✅ Операция подтверждена")
    details = _format_tool_status_details(payload)
    return f"{message} · {details}" if details else message


def _build_failure_tool_reply(tool_name: str, result: dict[str, Any], payload: Any) -> str:
    message = FAILURE_MESSAGE_BY_TOOL.get(tool_name, "❌ Операция завершилась ошибкой")
    reason = _extract_error_text(result, payload)
    return f"{message} · {reason}" if reason else message


def _format_tool_status_details(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    parts = []
    amount = payload.get("amount")
    currency = payload.get("currency")
    if amount not in (None, ""):
        amount_text = str(amount)
        if currency not in (None, ""):
            amount_text = f"{amount_text} {currency}"
        parts.append(amount_text)
    for key in ("category", "name"):
        value = payload.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    for key in ("spent_at", "id", "purchase_id", "imported_count"):
        value = payload.get(key)
        if value not in (None, "", False):
            label = "id" if key == "purchase_id" else key
            parts.append(f"{label}={value}")
    return " · ".join(parts)


def _extract_error_text(result: dict[str, Any], payload: Any) -> str:
    verification = result.get("verification")
    verification_reason = _extract_error_text_from_verification(verification)
    if verification_reason:
        return verification_reason
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
            pass

    purchase = payload.get("purchase")
    if isinstance(purchase, dict):
        for key in ("id", "purchase_id"):
            value = purchase.get(key)
            try:
                return int(value)
            except (TypeError, ValueError):
                pass

    return None


def _is_verified_purchase_result(
    result: dict[str, Any],
    payload: Any,
    purchase_id: int,
    *,
    user_id: int | str | None = None,
) -> bool:
    if not result.get("ok"):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") is False:
        return False
    actual_id = _extract_purchase_id(payload)
    if actual_id != purchase_id:
        return False
    purchase = _extract_purchase_payload(payload)
    if not isinstance(purchase, dict):
        return False
    if user_id is not None and str(purchase.get("user_id")) != str(user_id):
        return False
    return True


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
        "changed",
        "deleted_category",
        "move_purchases_to",
        "old_name",
        "new_name",
        "imported_count",
        "imported_purchase_ids",
    )
    return {key: payload.get(key) for key in keys if payload.get(key) not in (None, "", False)}


def _merge_verification(result: dict[str, Any], verification: Any) -> dict[str, Any]:
    merged = dict(result)
    merged["verification"] = verification
    return merged


def _build_verification_failure(
    *,
    tool_name: str,
    error: str,
    payload: Any,
    verification: Any,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "tool_name": tool_name,
        "payload": payload,
        "verification": verification,
    }


def _extract_purchase_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    purchase = payload.get("purchase")
    if isinstance(purchase, dict):
        return purchase
    if payload.get("id") is not None:
        return payload
    return None


def _extract_category_name(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    category = payload.get("category")
    if isinstance(category, dict):
        name = category.get("name")
        if name not in (None, ""):
            return str(name)
    for key in ("name", "category"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_rename_category_names(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    old_name = payload.get("old_name")
    new_name = payload.get("new_name")
    return _clean_optional_str(old_name), _clean_optional_str(new_name)


def _extract_delete_category_names(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    deleted_name = payload.get("deleted_category") or payload.get("name")
    target_name = payload.get("move_purchases_to")
    return _clean_optional_str(deleted_name), _clean_optional_str(target_name)


def _extract_imported_purchase_ids(payload: Any) -> list[int]:
    if not isinstance(payload, dict):
        return []
    raw_ids = payload.get("imported_purchase_ids")
    if not isinstance(raw_ids, list):
        return []
    result = []
    for value in raw_ids:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _is_verified_deleted_purchase_result(result: dict[str, Any], payload: Any) -> bool:
    if result.get("ok"):
        return False
    if isinstance(payload, dict) and payload.get("ok") is False:
        return True
    return True


def _is_verified_category_present(
    result: dict[str, Any],
    payload: Any,
    category_name: str,
    *,
    user_id: int | str | None = None,
) -> bool:
    if not result.get("ok"):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") is False:
        return False
    category = payload.get("category") if isinstance(payload.get("category"), dict) else payload
    if not isinstance(category, dict):
        return False
    if _clean_optional_str(category.get("name")) != category_name:
        return False
    if user_id is not None and str(category.get("user_id")) != str(user_id):
        return False
    return True


def _is_verified_category_absent(result: dict[str, Any], payload: Any) -> bool:
    if result.get("ok"):
        return False
    if isinstance(payload, dict) and payload.get("ok") is False:
        return True
    return True


def _extract_error_text_from_verification(verification: Any) -> str:
    if isinstance(verification, dict):
        payload = verification.get("payload")
        if isinstance(payload, dict):
            for key in ERROR_KEYS:
                value = payload.get(key)
                if value not in (None, ""):
                    return str(value)
        for key in ERROR_KEYS:
            value = verification.get(key)
            if value not in (None, ""):
                return str(value)
        for value in verification.values():
            nested = _extract_error_text_from_verification(value)
            if nested:
                return nested
    if isinstance(verification, list):
        for item in verification:
            nested = _extract_error_text_from_verification(item)
            if nested:
                return nested
    return ""


def _clean_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
