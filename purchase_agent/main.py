from __future__ import annotations

import asyncio
import logging

from purchase_agent.bot import run_bot
from purchase_agent.config import configure_logging, load_settings

logger = logging.getLogger(__name__)


async def async_main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger.info("Starting purchase Telegram agent")
    await run_bot(settings)


def run() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run()
