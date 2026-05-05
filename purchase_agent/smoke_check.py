from __future__ import annotations

import asyncio
import json

from purchase_agent.config import configure_logging, load_settings
from purchase_agent.llm_client import (
    _extract_purchase_id,
    _is_successful_write_result,
    _is_verified_purchase_result,
    _looks_like_category_sensitive_request,
)
from purchase_agent.mcp_client import PurchaseMCPClient


async def async_main() -> None:
    settings = load_settings(require_telegram=False, require_llm=False)
    configure_logging(settings.log_level)
    client = PurchaseMCPClient(settings)
    await client.connect()
    try:
        tools = await client.list_tools_for_prompt(refresh=True)
        health = await client.call_tool("health", {})
        print("MCP tools:")
        print(json.dumps([tool["name"] for tool in tools], ensure_ascii=False, indent=2))
        print("health:")
        print(json.dumps(health.get("payload", health), ensure_ascii=False, indent=2))
        print("tool schemas hide user_id:")
        print(
            json.dumps(
                {
                    tool["name"]: ("user_id" in ((tool.get("input_schema") or {}).get("properties") or {}))
                    for tool in tools
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print("category hint triggers:")
        samples = [
            "Добавь плату по кредитке суперсплит на 49471 рублей",
            "кофе 250",
            "поставь в категорию продукты",
        ]
        print(
            json.dumps(
                {sample: _looks_like_category_sensitive_request(sample.lower()) for sample in samples},
                ensure_ascii=False,
                indent=2,
            )
        )
        print("write result checks:")
        add_purchase_payload = {"id": 123, "amount": 399, "spent_at": "2026-05-05T10:00:00"}
        verification_payload = {"id": 123, "amount": 399, "spent_at": "2026-05-05T10:00:00"}
        failed_payload = {"message": "not saved"}
        print(
            json.dumps(
                {
                    "extract_purchase_id": _extract_purchase_id(add_purchase_payload),
                    "successful_add_purchase": _is_successful_write_result(
                        "add_purchase",
                        {"ok": True, "payload": add_purchase_payload},
                        add_purchase_payload,
                    ),
                    "failed_add_purchase": _is_successful_write_result(
                        "add_purchase",
                        {"ok": True, "payload": failed_payload},
                        failed_payload,
                    ),
                    "verified_add_purchase": _is_verified_purchase_result(
                        {"ok": True, "payload": verification_payload},
                        verification_payload,
                        123,
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await client.close()


def run() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run()
