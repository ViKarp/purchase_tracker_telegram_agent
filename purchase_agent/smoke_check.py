from __future__ import annotations

import asyncio
import json

from purchase_agent.config import configure_logging, load_settings
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
    finally:
        await client.close()


def run() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run()
