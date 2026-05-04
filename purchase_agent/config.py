from __future__ import annotations

import json
import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv


ToolMode = Literal["auto", "native", "json"]
AuthHeaderMode = Literal["api-key", "bearer"]


def _split_csv_ints(value: str | None) -> set[int]:
    if not value:
        return set()

    result: set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError as exc:
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS должен быть списком числовых Telegram id через запятую"
            ) from exc
    return result


def _env_json_dict(name: str) -> dict[str, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} должен быть валидным JSON-объектом") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} должен быть JSON-объектом")
    return {str(key): str(value) for key, value in parsed.items()}


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_allowed_user_ids: set[int]

    llm_base_url: str
    llm_api_key: str
    llm_project: str | None
    llm_model: str
    llm_auth_header_mode: AuthHeaderMode
    llm_tool_mode: ToolMode
    llm_temperature: float
    llm_max_tokens: int
    llm_max_tool_iterations: int

    mcp_server_command: str
    mcp_server_args: list[str]
    mcp_extra_env: dict[str, str]

    agent_timezone: str
    agent_default_currency: str
    agent_history_turns: int
    log_level: str

    project_root: Path = field(default_factory=lambda: Path.cwd())

    @property
    def llm_is_yandex_style(self) -> bool:
        return "cloud.yandex" in self.llm_base_url or self.llm_auth_header_mode == "api-key"


def load_settings() -> Settings:
    load_dotenv()

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not telegram_token:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в .env или переменных окружения")

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Не задан LLM_API_KEY в .env или переменных окружения")

    llm_project = os.environ.get("LLM_PROJECT", "").strip() or None
    yandex_folder_id = os.environ.get("YANDEX_FOLDER_ID", "").strip() or None
    effective_project = llm_project or yandex_folder_id

    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        if effective_project:
            model = f"gpt://{effective_project}/aliceai-llm"
        else:
            model = "aliceai-llm"

    auth_mode = os.environ.get("LLM_AUTH_HEADER_MODE", "api-key").strip().lower()
    if auth_mode not in {"api-key", "bearer"}:
        raise RuntimeError('LLM_AUTH_HEADER_MODE должен быть "api-key" или "bearer"')

    tool_mode = os.environ.get("LLM_TOOL_MODE", "auto").strip().lower()
    if tool_mode not in {"auto", "native", "json"}:
        raise RuntimeError('LLM_TOOL_MODE должен быть "auto", "native" или "json"')

    mcp_command = os.environ.get("MCP_SERVER_COMMAND", "purchase-tracker-mcp").strip()
    if not mcp_command:
        raise RuntimeError("MCP_SERVER_COMMAND не может быть пустым")

    mcp_args_raw = os.environ.get("MCP_SERVER_ARGS", "").strip()
    mcp_args = shlex.split(mcp_args_raw) if mcp_args_raw else []

    mcp_extra_env = _env_json_dict("MCP_ENV_JSON")
    purchase_db_path = os.environ.get("PURCHASE_DB_PATH", "").strip()
    if purchase_db_path:
        mcp_extra_env["PURCHASE_DB_PATH"] = purchase_db_path

    settings = Settings(
        telegram_bot_token=telegram_token,
        telegram_allowed_user_ids=_split_csv_ints(os.environ.get("TELEGRAM_ALLOWED_USER_IDS")),
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://llm.api.cloud.yandex.net/v1").strip(),
        llm_api_key=api_key,
        llm_project=effective_project,
        llm_model=model,
        llm_auth_header_mode=auth_mode,  # type: ignore[arg-type]
        llm_tool_mode=tool_mode,  # type: ignore[arg-type]
        llm_temperature=_float_env("LLM_TEMPERATURE", 0.1),
        llm_max_tokens=_int_env("LLM_MAX_TOKENS", 1200),
        llm_max_tool_iterations=_int_env("LLM_MAX_TOOL_ITERATIONS", 6),
        mcp_server_command=mcp_command,
        mcp_server_args=mcp_args,
        mcp_extra_env=mcp_extra_env,
        agent_timezone=os.environ.get("AGENT_TIMEZONE", "Europe/Vilnius").strip(),
        agent_default_currency=os.environ.get("AGENT_DEFAULT_CURRENCY", "RUB").strip().upper(),
        agent_history_turns=_int_env("AGENT_HISTORY_TURNS", 10),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
    )
    return settings


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
