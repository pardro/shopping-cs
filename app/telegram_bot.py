import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.container import get_main_agent
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self) -> None:
        self._settings = get_settings()
        if not self._settings.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self._base_url = f"https://api.telegram.org/bot{self._settings.telegram_bot_token}"
        self._agent = get_main_agent()

    async def run(self) -> None:
        offset = 0
        async with httpx.AsyncClient(timeout=None) as client:
            logger.info("Telegram polling started")
            while True:
                updates = await self._get_updates(client, offset)
                for update in updates:
                    offset = max(offset, update["update_id"] + 1)
                    await self._handle_update(client, update)

    async def _get_updates(self, client: httpx.AsyncClient, offset: int) -> list[dict[str, Any]]:
        response = await client.get(
            f"{self._base_url}/getUpdates",
            params={
                "offset": offset,
                "timeout": self._settings.telegram_poll_timeout_seconds,
                "allowed_updates": ["message"],
            },
        )
        response.raise_for_status()
        return response.json().get("result", [])

    async def _handle_update(self, client: httpx.AsyncClient, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = message.get("text") or ""
        if not chat_id or not text:
            return
        if not self._is_allowed(chat_id):
            await self._send_message(client, chat_id, "허용되지 않은 Telegram chat_id입니다.")
            return
        result = await self._agent.handle_message(text, user_key=f"telegram:{chat_id}")
        await self._send_message(client, chat_id, result.message)

    async def _send_message(self, client: httpx.AsyncClient, chat_id: int, text: str) -> None:
        response = await client.post(
            f"{self._base_url}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4096]},
        )
        response.raise_for_status()

    def _is_allowed(self, chat_id: int) -> bool:
        allowed = self._settings.allowed_telegram_chat_ids
        return not allowed or chat_id in allowed


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.app_log_level)
    await TelegramBot().run()


if __name__ == "__main__":
    asyncio.run(main())
