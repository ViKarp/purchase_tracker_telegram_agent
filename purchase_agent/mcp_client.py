from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from purchase_agent.config import Settings

logger = logging.getLogger(__name__)


class PurchaseMCPClient:
    """Long-lived stdio MCP client for the local purchase-tracker server."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._lock = asyncio.Lock()
        self._tools_cache: list[Any] | None = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCP-сессия ещё не подключена")
        return self._session

    async def connect(self) -> None:
        if self._session is not None:
            return

        env = os.environ.copy()
        env.update(self._settings.mcp_extra_env)

        server_params = StdioServerParameters(
            command=self._settings.mcp_server_command,
            args=self._settings.mcp_server_args,
            env=env,
        )

        self._exit_stack = AsyncExitStack()
        try:
            transport = await self._exit_stack.enter_async_context(stdio_client(server_params))
            read_stream, write_stream = transport
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._session.initialize()
            logger.info("Connected to MCP server: %s", self._settings.mcp_server_command)
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._session = None
        self._tools_cache = None

    async def list_tools(self, *, refresh: bool = False) -> list[Any]:
        async with self._lock:
            if self._tools_cache is not None and not refresh:
                return self._tools_cache
            result = await self.session.list_tools()
            self._tools_cache = list(result.tools)
            return self._tools_cache

    async def list_tools_openai_schema(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        tools = await self.list_tools(refresh=refresh)
        converted: list[dict[str, Any]] = []
        for tool in tools:
            input_schema = getattr(tool, "inputSchema", None) or {
                "type": "object",
                "properties": {},
            }
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": input_schema,
                    },
                }
            )
        return converted

    async def list_tools_for_prompt(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        tools = await self.list_tools(refresh=refresh)
        result: list[dict[str, Any]] = []
        for tool in tools:
            result.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": getattr(tool, "inputSchema", None) or {},
                }
            )
        return result

    async def tool_accepts_argument(self, tool_name: str, argument_name: str) -> bool:
        tools = await self.list_tools()
        for tool in tools:
            if tool.name != tool_name:
                continue

            input_schema = getattr(tool, "inputSchema", None) or {}
            properties = input_schema.get("properties", {})
            return argument_name in properties

        return False

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        payload = dict(arguments or {})
        if (
            user_id is not None
            and "user_id" not in payload
            and await self.tool_accepts_argument(name, "user_id")
        ):
            payload["user_id"] = str(user_id)
        logger.debug("Calling MCP tool name=%s payload=%s", name, _truncate_for_log(payload))
        async with self._lock:
            result = await self.session.call_tool(name, payload)
        serialized = serialize_call_tool_result(result)
        logger.debug("MCP tool completed name=%s result=%s", name, _truncate_for_log(serialized))
        return serialized


async def safe_call_tool(
    mcp_client: PurchaseMCPClient,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    user_id: int | str | None = None,
) -> dict[str, Any]:
    try:
        return await mcp_client.call_tool(name, arguments or {}, user_id=user_id)
    except Exception as exc:
        logger.exception("MCP tool call failed: %s", name)
        return {"ok": False, "error": str(exc), "tool_name": name}


def serialize_call_tool_result(result: Any) -> dict[str, Any]:
    """Convert MCP CallToolResult to a JSON-serializable dict."""
    is_error = bool(getattr(result, "isError", False))
    content_items = []

    for item in getattr(result, "content", []) or []:
        item_type = getattr(item, "type", None)
        if item_type == "text" and hasattr(item, "text"):
            text = item.text
            parsed = _try_parse_json(text)
            content_items.append({"type": "text", "text": text, "json": parsed})
        elif hasattr(item, "model_dump"):
            content_items.append(item.model_dump(mode="json"))
        else:
            content_items.append({"type": str(item_type or "unknown"), "repr": repr(item)})

    compact_payload = _extract_single_json_payload(content_items)
    return {
        "ok": not is_error,
        "is_error": is_error,
        "content": content_items,
        "payload": compact_payload,
    }


def result_to_text(result: dict[str, Any]) -> str:
    payload = result.get("payload")
    if payload is not None:
        return json.dumps(payload, ensure_ascii=False, default=str)
    return json.dumps(result, ensure_ascii=False, default=str)


def _try_parse_json(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _extract_single_json_payload(content_items: list[dict[str, Any]]) -> Any | None:
    if len(content_items) != 1:
        return None
    item = content_items[0]
    if item.get("json") is not None:
        return item["json"]
    return None


def _truncate_for_log(value: Any, limit: int = 1200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
